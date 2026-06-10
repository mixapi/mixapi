from __future__ import annotations

import hashlib

import redis

from mixapi.circuits import is_transient_failure
from mixapi.redis_scripts import LuaScript


_IS_OPEN = """
local opened = redis.call('HGET', KEYS[1], 'opened_ms')
if not opened then return 0 end
local now = redis.call('TIME')
local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
if now_ms - tonumber(opened) >= tonumber(ARGV[1]) then
  redis.call('DEL', KEYS[1])
  return 0
end
return 1
"""

_FAILURE = """
local failures = redis.call('HINCRBY', KEYS[1], 'failures', 1)
if failures >= tonumber(ARGV[1]) then
  local now = redis.call('TIME')
  local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
  redis.call('HSET', KEYS[1], 'opened_ms', now_ms)
end
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
return failures
"""


class RedisCircuitBreaker:
    def __init__(
        self,
        client: redis.Redis,
        *,
        namespace: str,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 30.0,
    ) -> None:
        self._client = client
        self._namespace = namespace
        self.failure_threshold = failure_threshold
        self.recovery_timeout_ms = max(int(recovery_timeout_seconds * 1000), 1)
        self._is_open = LuaScript(client, _IS_OPEN)
        self._failure = LuaScript(client, _FAILURE)

    def is_open(self, provider: str, provider_model: str) -> bool:
        return bool(
            self._is_open(
                [self._key(provider, provider_model)],
                [self.recovery_timeout_ms],
            )
        )

    def record_failure(self, provider: str, provider_model: str, reason: str) -> None:
        if not is_transient_failure(reason):
            return
        self._failure(
            [self._key(provider, provider_model)],
            [self.failure_threshold, max(self.recovery_timeout_ms * 2, 60_000)],
        )

    def record_success(self, provider: str, provider_model: str) -> None:
        try:
            self._client.delete(self._key(provider, provider_model))
        except (redis.RedisError, OSError) as error:
            from mixapi.errors import control_plane_unavailable

            raise control_plane_unavailable() from error

    def _key(self, provider: str, provider_model: str) -> str:
        digest = hashlib.sha256(f"{provider}\0{provider_model}".encode()).hexdigest()
        return f"{self._namespace}:v1:circuit:{digest}"
