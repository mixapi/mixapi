from __future__ import annotations

import redis

from mixapi.postgres import DependencyUnavailable
from mixapi.settings import Settings


class RedisRuntime:
    def __init__(self, settings: Settings) -> None:
        timeout = min(
            settings.redis_socket_timeout_seconds,
            settings.dependency_connect_timeout_seconds,
        )
        self._client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
        )
        self._closed = True

    @property
    def client(self) -> redis.Redis:
        return self._client

    @property
    def closed(self) -> bool:
        return self._closed

    def open(self) -> None:
        try:
            if self._client.ping() is not True:
                raise redis.RedisError("Redis PING did not return true")
        except (redis.RedisError, OSError) as error:
            self._client.close()
            self._closed = True
            raise DependencyUnavailable("Redis is unavailable") from error
        self._closed = False

    def close(self) -> None:
        self._client.close()
        self._closed = True

    def ping(self) -> bool:
        try:
            return self._client.ping() is True
        except (redis.RedisError, OSError) as error:
            raise DependencyUnavailable("Redis is unavailable") from error
