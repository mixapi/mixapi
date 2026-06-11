from __future__ import annotations

from typing import Protocol

import redis


class StickySessionStore(Protocol):
    def get_connection_id(self, tenant_id: str, session_id: str) -> str | None: ...
    def set_connection_id(self, tenant_id: str, session_id: str, connection_id: str) -> None: ...


class RedisStickySessionStore:
    def __init__(self, client: redis.Redis, namespace: str, ttl_seconds: int = 3600) -> None:
        self._client = client
        self._namespace = namespace
        self._ttl = ttl_seconds

    def _key(self, tenant_id: str, session_id: str) -> str:
        return f"{self._namespace}:v1:session:{tenant_id}:{session_id}"

    def get_connection_id(self, tenant_id: str, session_id: str) -> str | None:
        value = self._client.get(self._key(tenant_id, session_id))
        if value:
            self._client.expire(self._key(tenant_id, session_id), self._ttl)
            if isinstance(value, bytes):
                return value.decode("utf-8")
            return str(value)
        return None

    def set_connection_id(self, tenant_id: str, session_id: str, connection_id: str) -> None:
        self._client.setex(
            self._key(tenant_id, session_id),
            self._ttl,
            connection_id,
        )
