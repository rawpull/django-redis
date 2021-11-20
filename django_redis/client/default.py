import random
import re
import socket
import typing
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Union, TypeVar, Tuple, Type, Set

from django.conf import settings
from django.core.cache.backends.base import DEFAULT_TIMEOUT, BaseCache, get_key_func
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string
from redis import Redis
from redis.exceptions import ConnectionError, ResponseError, TimeoutError

from .. import pool
from ..exceptions import CompressorError, ConnectionInterrupted
from ..util import CacheKey

_main_exceptions = (TimeoutError, ResponseError, ConnectionError, socket.timeout)

special_re = re.compile("([*?[])")


def glob_escape(s: str) -> str:
    return special_re.sub(r"[\1]", s)


class DefaultClient:
    def __init__(self, server, params: Dict[str, Any], backend: BaseCache) -> None:
        self._backend = backend
        self._server = server
        self._params = params

        self.reverse_key = get_key_func(
            params.get("REVERSE_KEY_FUNCTION")
            or "django_redis.util.default_reverse_key"
        )

        if not self._server:
            raise ImproperlyConfigured("Missing connections string")

        if not isinstance(self._server, (list, tuple, set)):
            self._server = self._server.split(",")

        self._clients: List[Optional[Redis]] = [None] * len(self._server)
        self._options = params.get("OPTIONS", {})
        self._replica_read_only = self._options.get("REPLICA_READ_ONLY", True)

        serializer_path = self._options.get(
            "SERIALIZER", "django_redis.serializers.pickle.PickleSerializer"
        )
        serializer_cls = import_string(serializer_path)

        compressor_path = self._options.get(
            "COMPRESSOR", "django_redis.compressors.identity.IdentityCompressor"
        )
        compressor_cls = import_string(compressor_path)

        self._serializer = serializer_cls(options=self._options)
        self._compressor = compressor_cls(options=self._options)

        self.connection_factory = pool.get_connection_factory(options=self._options)

    def __contains__(self, key: Any) -> bool:
        return self.has_key(key)

    def get_next_client_index(
            self, write: bool = True, tried: Optional[List[int]] = None
    ) -> int:
        """
        Return a next index for read client. This function implements a default
        behavior for get a next read client for a replication setup.

        Overwrite this function if you want a specific
        behavior.
        """
        if tried is None:
            tried = list()

        if tried and len(tried) < len(self._server):
            not_tried = [i for i in range(0, len(self._server)) if i not in tried]
            return random.choice(not_tried)

        if write or len(self._server) == 1:
            return 0

        return random.randint(1, len(self._server) - 1)

    def get_client(
            self,
            write: bool = True,
            tried: Optional[List[int]] = None,
            show_index: bool = False,
    ):
        """
        Method used for obtain a raw redis client.

        This function is used by almost all cache backend
        operations for obtain a native redis client/connection
        instance.
        """
        index = self.get_next_client_index(write=write, tried=tried)

        if self._clients[index] is None:
            self._clients[index] = self.connect(index)

        if show_index:
            return self._clients[index], index
        else:
            return self._clients[index]

    def connect(self, index: int = 0) -> Redis:
        """
        Given a connection index, returns a new raw redis client/connection
        instance. Index is used for replication setups and indicates that
        connection string should be used. In normal setups, index is 0.
        """
        return self.connection_factory.connect(self._server[index])

    def disconnect(self, index=0, client=None):
        """delegates the connection factory to disconnect the client"""
        if not client:
            client = self._clients[index]
        return self.connection_factory.disconnect(client) if client else None

    def execute_command(self, command: str, *args, **kwargs) -> Any:
        client: Redis = kwargs.pop('client', None)
        write = kwargs.pop('write', False)
        if client is None:
            client = self.get_client(write=write)
        return client.execute_command(command, *args, **kwargs)

    def list_pop(self, key: str, desc: bool = False, version: str = None,
                 client: Redis = None) -> Any:
        if client is None:
            client = self.get_client(write=True)
        return client.rpop(self.make_key(key, version=version)) \
            if desc else client.lpop(self.make_key(key, version=version))

    def list_push(self, key: str, values: Any, desc: bool = False, version: str = None,
                  client: Redis = None) -> int:
        if client is None:
            client = self.get_client(write=True)
        return client.lpush(self.make_key(key, version=version), *map(self.encode, values))

    def list_range(self, key: str, start: int = 0, end: int = -1, version: str = None,
                   client: Redis = None) -> List[Any]:
        if client is None:
            client = self.get_client()
        return list(map(self.decode, client.lrange(self.make_key(key, version=version), start=start, end=end)))

    def list_index(self, key: str, index: int, version: str = None, client: Redis = None) -> Any:
        if client is None:
            client = self.get_client()
        return self.decode(client.lindex(self.make_key(key, version=version), index=index))

    def set_add(self, key: str, mapping: typing.Dict[str, Any], nx: bool = False, xx: bool = False,
                version: str = None, client: Redis = None) -> bool:
        if not isinstance(mapping, dict):
            raise TypeError
        if nx and xx:
            raise ValueError
        args = []

        if nx:
            args.append('NX')
        if xx:
            args.append('XX')

        [args.extend([score, self.encode(data)])
         for data, score in mapping.items()]
        return self.execute_command('ZADD', self.make_key(key, version=version), *args, client=client, write=True)

    def set_count(self, key: str, min_score: int = None, max_score: int = None, version: str = None,
                  client: Redis = None) -> int:
        # @TODO By score
        if client is None:
            client = self.get_client()
        return client.zcount(name=self.make_key(key, version=version),
                             min=min_score or '-inf',
                             max=max_score or '+inf')

    def set_range(self, key: str, start: int, end: int, desc: bool = False, with_scores: bool = False,
                  score_cast: type = float, version: str = None, client: Redis = None
                  ) -> Union[Dict[Any, Type[Any]], Set[Any]]:
        if client is None:
            client = self.get_client()
        encoded = client.zrange(
            name=self.make_key(key, version=version),
            start=start, end=end, desc=desc, withscores=with_scores,
            score_cast_func=score_cast)
        return {self.decode(item[0]): item[1] for item in encoded} \
            if with_scores else {self.decode(item) for item in encoded}

    def set_remove(self, key: str, items: List[str], version: str = None, client: Redis = None) -> bool:
        # @ TODO All versions

        pass

    def rename(self, key: str, new_key: str, version: str = None, client: Redis = None) -> bool:
        if client is None:
            client = self.get_client()
        return client.rename(self.make_key(key, version=version), self.make_key(new_key, version=version)) == 1

    def set(
            self,
            key: str,
            value: Any,
            timeout: Optional[float] = DEFAULT_TIMEOUT,
            version: Optional[int] = None,
            client: Optional[Redis] = None,
            nx: bool = False,
            xx: bool = False,
    ) -> bool:
        """
        Persist a value to the cache, and set an optional expiration time.

        Also supports optional nx parameter. If set to True - will use redis
        setnx instead of set.
        """
        nkey = self.make_key(key, version=version)
        nvalue = self.encode(value)

        if timeout is DEFAULT_TIMEOUT:
            timeout = self._backend.default_timeout

        original_client = client
        tried: List[int] = []
        while True:
            try:
                if client is None:
                    client, index = self.get_client(
                        write=True, tried=tried, show_index=True
                    )

                if timeout is not None:
                    # Convert to milliseconds
                    timeout = int(timeout * 1000)

                    if timeout <= 0:
                        if nx:
                            # Using negative timeouts when nx is True should
                            # not expire (in our case delete) the value if it exists.
                            # Obviously expire not existent value is noop.
                            return not self.has_key(key, version=version, client=client)
                        else:
                            # redis doesn't support negative timeouts in ex flags
                            # so it seems that it's better to just delete the key
                            # than to set it and than expire in a pipeline
                            return bool(
                                self.delete(key, client=client, version=version)
                            )

                return bool(client.set(nkey, nvalue, nx=nx, px=timeout, xx=xx))
            except _main_exceptions as e:
                if (
                        not original_client
                        and not self._replica_read_only
                        and len(tried) < len(self._server)
                ):
                    tried.append(index)
                    client = None
                    continue
                raise ConnectionInterrupted(connection=client) from e

    def incr_version(
            self,
            key: Any,
            delta: int = 1,
            version: Optional[int] = None,
            client: Optional[Redis] = None,
    ) -> int:
        """
        Adds delta to the cache version for the supplied key. Returns the
        new version.
        """

        if client is None:
            client = self.get_client(write=True)

        if version is None:
            version = self._backend.version

        old_key = self.make_key(key, version)
        value = self.get(old_key, version=version, client=client)

        try:
            ttl = self.ttl(old_key, version=version, client=client)
        except _main_exceptions as e:
            raise ConnectionInterrupted(connection=client) from e

        if value is None:
            raise ValueError("Key '%s' not found" % key)

        if isinstance(key, CacheKey):
            new_key = self.make_key(key.original_key(), version=version + delta)
        else:
            new_key = self.make_key(key, version=version + delta)

        self.set(new_key, value, timeout=ttl, client=client)
        self.delete(old_key, client=client)
        return version + delta

    def add(
            self,
            key: Any,
            value: Any,
            timeout: Any = DEFAULT_TIMEOUT,
            version: Optional[Any] = None,
            client: Optional[Redis] = None,
    ) -> bool:
        """
        Add a value to the cache, failing if the key already exists.

        Returns ``True`` if the object was added, ``False`` if not.
        """
        return self.set(key, value, timeout, version=version, client=client, nx=True)

    def get(
            self,
            key: Any,
            default=None,
            version: Optional[int] = None,
            client: Optional[Redis] = None,
    ) -> Any:
        """
        Retrieve a value from the cache.

        Returns decoded value if key is found, the default if not.
        """
        if client is None:
            client = self.get_client(write=False)

        key = self.make_key(key, version=version)

        try:
            value = client.get(key)
        except _main_exceptions as e:
            raise ConnectionInterrupted(connection=client) from e

        if value is None:
            return default

        return self.decode(value)

    def persist(
            self, key: Any, version: Optional[int] = None, client: Optional[Redis] = None
    ) -> bool:
        if client is None:
            client = self.get_client(write=True)

        key = self.make_key(key, version=version)

        return client.persist(key)

    def expire(
            self,
            key: Any,
            timeout,
            version: Optional[int] = None,
            client: Optional[Redis] = None,
    ) -> bool:
        if client is None:
            client = self.get_client(write=True)

        key = self.make_key(key, version=version)

        return client.expire(key, timeout)

    def pexpire(self, key, timeout, version=None, client=None) -> bool:
        if client is None:
            client = self.get_client(write=True)

        key = self.make_key(key, version=version)

        # Temporary casting until https://github.com/redis/redis-py/issues/1664
        # is fixed.
        return bool(client.pexpire(key, timeout))

    def pexpire_at(
            self,
            key: Any,
            when: Union[datetime, int],
            version: Optional[int] = None,
            client: Optional[Redis] = None,
    ) -> bool:
        """
        Set an expire flag on a ``key`` to ``when``, which can be represented
        as an integer indicating unix time or a Python datetime object.
        """
        if client is None:
            client = self.get_client(write=True)

        key = self.make_key(key, version=version)

        return bool(client.pexpireat(key, when))

    def expire_at(
            self,
            key: Any,
            when: Union[datetime, int],
            version: Optional[int] = None,
            client: Optional[Redis] = None,
    ) -> bool:
        """
        Set an expire flag on a ``key`` to ``when``, which can be represented
        as an integer indicating unix time or a Python datetime object.
        """
        if client is None:
            client = self.get_client(write=True)

        key = self.make_key(key, version=version)

        return client.expireat(key, when)

    def lock(
            self,
            key,
            version: Optional[int] = None,
            timeout=None,
            sleep=0.1,
            blocking_timeout=None,
            client: Optional[Redis] = None,
            thread_local=True,
    ):
        if client is None:
            client = self.get_client(write=True)

        key = self.make_key(key, version=version)
        return client.lock(
            key,
            timeout=timeout,
            sleep=sleep,
            blocking_timeout=blocking_timeout,
            thread_local=thread_local,
        )

    def delete(
            self,
            key: Any,
            version: Optional[int] = None,
            prefix: Optional[str] = None,
            client: Optional[Redis] = None,
    ) -> int:
        """
        Remove a key from the cache.
        """
        if client is None:
            client = self.get_client(write=True)

        try:
            return client.delete(self.make_key(key, version=version, prefix=prefix))
        except _main_exceptions as e:
            raise ConnectionInterrupted(connection=client) from e

    def delete_pattern(
            self,
            pattern: str,
            version: Optional[int] = None,
            prefix: Optional[str] = None,
            client: Optional[Redis] = None,
            itersize: Optional[int] = None,
    ) -> int:
        """
        Remove all keys matching pattern.
        """

        if client is None:
            client = self.get_client(write=True)

        pattern = self.make_pattern(pattern, version=version, prefix=prefix)

        try:
            count = 0
            for key in client.scan_iter(match=pattern, count=itersize):
                client.delete(key)
                count += 1
            return count
        except _main_exceptions as e:
            raise ConnectionInterrupted(connection=client) from e

    def delete_many(
            self, keys, version: Optional[int] = None, client: Optional[Redis] = None
    ):
        """
        Remove multiple keys at once.
        """

        if client is None:
            client = self.get_client(write=True)

        keys = [self.make_key(k, version=version) for k in keys]

        if not keys:
            return

        try:
            return client.delete(*keys)
        except _main_exceptions as e:
            raise ConnectionInterrupted(connection=client) from e

    def clear(self, client: Optional[Redis] = None) -> None:
        """
        Flush all cache keys.
        """

        if client is None:
            client = self.get_client(write=True)

        try:
            client.flushdb()
        except _main_exceptions as e:
            raise ConnectionInterrupted(connection=client) from e

    def decode(self, value: Union[bytes, int]) -> Any:
        """
        Decode the given value.
        """
        try:
            value = int(value)
        except (ValueError, TypeError):
            try:
                value = self._compressor.decompress(value)
            except CompressorError:
                # Handle little values, chosen to be not compressed
                pass
            value = self._serializer.loads(value)
        return value

    def encode(self, value: Any) -> Union[bytes, Any]:
        """
        Encode the given value.
        """

        if isinstance(value, bool) or not isinstance(value, int):
            value = self._serializer.dumps(value)
            value = self._compressor.compress(value)
            return value

        return value

    def get_many(
            self, keys, version: Optional[int] = None, client: Optional[Redis] = None
    ) -> OrderedDict:
        """
        Retrieve many keys.
        """

        if client is None:
            client = self.get_client(write=False)

        if not keys:
            return OrderedDict()

        recovered_data = OrderedDict()

        map_keys = OrderedDict((self.make_key(k, version=version), k) for k in keys)

        try:
            results = client.mget(*map_keys)
        except _main_exceptions as e:
            raise ConnectionInterrupted(connection=client) from e

        for key, value in zip(map_keys, results):
            if value is None:
                continue
            recovered_data[map_keys[key]] = self.decode(value)
        return recovered_data

    def set_many(
            self,
            data: Dict[Any, Any],
            timeout: Optional[float] = DEFAULT_TIMEOUT,
            version: Optional[int] = None,
            client: Optional[Redis] = None,
    ) -> None:
        """
        Set a bunch of values in the cache at once from a dict of key/value
        pairs. This is much more efficient than calling set() multiple times.

        If timeout is given, that timeout will be used for the key; otherwise
        the default cache timeout will be used.
        """
        if client is None:
            client = self.get_client(write=True)

        try:
            pipeline = client.pipeline()
            for key, value in data.items():
                self.set(key, value, timeout, version=version, client=pipeline)
            pipeline.execute()
        except _main_exceptions as e:
            raise ConnectionInterrupted(connection=client) from e

    def _incr(
            self,
            key: Any,
            delta: int = 1,
            version: Optional[int] = None,
            client: Optional[Redis] = None,
            ignore_key_check: bool = False,
    ) -> int:
        if client is None:
            client = self.get_client(write=True)

        key = self.make_key(key, version=version)

        try:
            try:
                # if key expired after exists check, then we get
                # key with wrong value and ttl -1.
                # use lua script for atomicity
                if not ignore_key_check:
                    lua = """
                    local exists = redis.call('EXISTS', KEYS[1])
                    if (exists == 1) then
                        return redis.call('INCRBY', KEYS[1], ARGV[1])
                    else return false end
                    """
                else:
                    lua = """
                    return redis.call('INCRBY', KEYS[1], ARGV[1])
                    """
                value = client.eval(lua, 1, key, delta)
                if value is None:
                    raise ValueError("Key '%s' not found" % key)
            except ResponseError:
                # if cached value or total value is greater than 64 bit signed
                # integer.
                # elif int is encoded. so redis sees the data as string.
                # In this situations redis will throw ResponseError

                # try to keep TTL of key
                timeout = self.ttl(key, version=version, client=client)

                # returns -2 if the key does not exist
                # means, that key have expired
                if timeout == -2:
                    raise ValueError("Key '%s' not found" % key)
                value = self.get(key, version=version, client=client) + delta
                self.set(key, value, version=version, timeout=timeout, client=client)
        except _main_exceptions as e:
            raise ConnectionInterrupted(connection=client) from e

        return value

    def incr(
            self,
            key: Any,
            delta: int = 1,
            version: Optional[int] = None,
            client: Optional[Redis] = None,
            ignore_key_check: bool = False,
    ) -> int:
        """
        Add delta to value in the cache. If the key does not exist, raise a
        ValueError exception. if ignore_key_check=True then the key will be
        created and set to the delta value by default.
        """
        return self._incr(
            key=key,
            delta=delta,
            version=version,
            client=client,
            ignore_key_check=ignore_key_check,
        )

    def decr(
            self,
            key: Any,
            delta: int = 1,
            version: Optional[int] = None,
            client: Optional[Redis] = None,
    ) -> int:
        """
        Decreace delta to value in the cache. If the key does not exist, raise a
        ValueError exception.
        """
        return self._incr(key=key, delta=-delta, version=version, client=client)

    def ttl(
            self, key: Any, version: Optional[int] = None, client: Optional[Redis] = None
    ) -> Optional[int]:
        """
        Executes TTL redis command and return the "time-to-live" of specified key.
        If key is a non volatile key, it returns None.
        """
        if client is None:
            client = self.get_client(write=False)

        key = self.make_key(key, version=version)
        if not client.exists(key):
            return 0

        t = client.ttl(key)

        if t >= 0:
            return t
        elif t == -1:
            return None
        elif t == -2:
            return 0
        else:
            # Should never reach here
            return None

    def pttl(self, key, version=None, client=None):
        """
        Executes PTTL redis command and return the "time-to-live" of specified key.
        If key is a non volatile key, it returns None.
        """
        if client is None:
            client = self.get_client(write=False)

        key = self.make_key(key, version=version)
        if not client.exists(key):
            return 0

        t = client.pttl(key)

        if t >= 0:
            return t
        elif t == -1:
            return None
        elif t == -2:
            return 0
        else:
            # Should never reach here
            return None

    def has_key(
            self, key: Any, version: Optional[int] = None, client: Optional[Redis] = None
    ) -> bool:
        """
        Test if key exists.
        """

        if client is None:
            client = self.get_client(write=False)

        key = self.make_key(key, version=version)
        try:
            return client.exists(key) == 1
        except _main_exceptions as e:
            raise ConnectionInterrupted(connection=client) from e

    def iter_keys(
            self,
            search: str,
            itersize: Optional[int] = None,
            client: Optional[Redis] = None,
            version: Optional[int] = None,
    ) -> Iterator[str]:
        """
        Same as keys, but uses redis >= 2.8 cursors
        for make memory efficient keys iteration.
        """

        if client is None:
            client = self.get_client(write=False)

        pattern = self.make_pattern(search, version=version)
        for item in client.scan_iter(match=pattern, count=itersize):
            yield self.reverse_key(item.decode())

    def keys(
            self, search: str, version: Optional[int] = None, client: Optional[Redis] = None
    ) -> List[Any]:
        """
        Execute KEYS command and return matched results.
        Warning: this can return huge number of results, in
        this case, it strongly recommended use iter_keys
        for it.
        """

        if client is None:
            client = self.get_client(write=False)

        pattern = self.make_pattern(search, version=version)
        try:
            return [self.reverse_key(k.decode()) for k in client.keys(pattern)]
        except _main_exceptions as e:
            raise ConnectionInterrupted(connection=client) from e

    def make_key(
            self, key: Any, version: Optional[Any] = None, prefix: Optional[str] = None
    ) -> CacheKey:
        if isinstance(key, CacheKey):
            return key

        if prefix is None:
            prefix = self._backend.key_prefix

        if version is None:
            version = self._backend.version

        return CacheKey(self._backend.key_func(key, prefix, version))

    def make_pattern(
            self, pattern: str, version: Optional[int] = None, prefix: Optional[str] = None
    ) -> CacheKey:
        if isinstance(pattern, CacheKey):
            return pattern

        if prefix is None:
            prefix = self._backend.key_prefix
        prefix = glob_escape(prefix)

        if version is None:
            version = self._backend.version
        version_str = glob_escape(str(version))

        return CacheKey(self._backend.key_func(pattern, prefix, version_str))

    def close(self, **kwargs):
        close_flag = self._options.get(
            "CLOSE_CONNECTION",
            getattr(settings, "DJANGO_REDIS_CLOSE_CONNECTION", False),
        )
        if close_flag:
            self.do_close_clients()

    def do_close_clients(self):
        """default implementation: Override in custom client"""
        num_clients = len(self._clients)
        for idx in range(num_clients):
            self.disconnect(index=idx)
        self._clients = [None] * num_clients

    def touch(
            self,
            key: Any,
            timeout: Optional[float] = DEFAULT_TIMEOUT,
            version: Optional[int] = None,
            client: Optional[Redis] = None,
    ) -> bool:
        """
        Sets a new expiration for a key.
        """

        if timeout is DEFAULT_TIMEOUT:
            timeout = self._backend.default_timeout

        if client is None:
            client = self.get_client(write=True)

        key = self.make_key(key, version=version)
        if timeout is None:
            return bool(client.persist(key))
        else:
            # Convert to milliseconds
            timeout = int(timeout * 1000)
            return bool(client.pexpire(key, timeout))
