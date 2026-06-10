from __future__ import annotations

import hashlib
import json
from typing import Any

import redis

from mixapi.auth import Principal
from mixapi.errors import control_plane_unavailable, validation_error
from mixapi.idempotency import IdempotencyReplay, request_hash
from mixapi.redis_scripts import LuaScript


_STORE = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return 1
end
return 0
"""


class RedisIdempotencyStore:
    def __init__(
        self,
        client: redis.Redis,
        *,
        namespace: str,
        ttl_seconds: int,
    ) -> None:
        self._client = client
        self._namespace = namespace
        self._ttl_seconds = ttl_seconds
        self._store_script = LuaScript(client, _STORE)

    def replay(
        self,
        principal: Principal,
        endpoint: str,
        key: str | None,
        request_body: dict[str, Any],
    ) -> IdempotencyReplay | None:
        if not key:
            return None
        try:
            raw = self._client.get(self._key(principal.tenant_id, endpoint, key))
        except (redis.RedisError, OSError) as error:
            raise control_plane_unavailable() from error
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            if payload["version"] != 1:
                raise ValueError("unsupported idempotency record")
            stored_hash = payload["request_hash"]
            response = payload["response"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise control_plane_unavailable(
                message="The idempotency record is unavailable."
            ) from error
        if stored_hash != request_hash(request_body):
            raise validation_error(
                "idempotency_key_reused",
                "Idempotency key was reused with a different request body.",
            )
        return IdempotencyReplay(response=response)

    def store(
        self,
        principal: Principal,
        endpoint: str,
        key: str | None,
        request_body: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        if not key:
            return
        payload = json.dumps(
            {
                "version": 1,
                "request_hash": request_hash(request_body),
                "response": response,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self._store_script(
            [self._key(principal.tenant_id, endpoint, key)],
            [payload, self._ttl_seconds],
        )

    def _key(self, tenant_id: str, endpoint: str, key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"{self._namespace}:v1:idempotency:{tenant_id}:{endpoint}:{digest}"
