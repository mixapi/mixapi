from __future__ import annotations

import uuid

import redis

from mixapi.auth import Principal
from mixapi.errors import quota_exceeded
from mixapi.quota import TokenReservation
from mixapi.redis_scripts import LuaScript


_RESERVE_REQUEST = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= tonumber(ARGV[1]) then return 0 end
redis.call('INCR', KEYS[1])
return 1
"""

_RESERVE_TOKENS = """
local actual = tonumber(redis.call('GET', KEYS[1]) or '0')
local reserved = tonumber(redis.call('GET', KEYS[2]) or '0')
local amount = tonumber(ARGV[1])
if actual + reserved + amount > tonumber(ARGV[2]) then return 0 end
redis.call('SET', KEYS[3], amount)
redis.call('INCRBY', KEYS[2], amount)
return 1
"""

_RECONCILE_TOKENS = """
local amount = redis.call('GET', KEYS[3])
if not amount then return 0 end
redis.call('DEL', KEYS[3])
redis.call('DECRBY', KEYS[2], tonumber(amount))
redis.call('INCRBY', KEYS[1], tonumber(ARGV[1]))
return 1
"""

_RELEASE_TOKENS = """
local amount = redis.call('GET', KEYS[2])
if not amount then return 0 end
redis.call('DEL', KEYS[2])
redis.call('DECRBY', KEYS[1], tonumber(amount))
return 1
"""


class RedisQuotaService:
    def __init__(
        self,
        client: redis.Redis,
        *,
        namespace: str,
        request_limit: int | None = None,
        token_limit: int | None = None,
    ) -> None:
        self._namespace = namespace
        self.request_limit = request_limit
        self.token_limit = token_limit
        self._reserve_request = LuaScript(client, _RESERVE_REQUEST)
        self._reserve_tokens = LuaScript(client, _RESERVE_TOKENS)
        self._reconcile_tokens = LuaScript(client, _RECONCILE_TOKENS)
        self._release_tokens = LuaScript(client, _RELEASE_TOKENS)

    def reserve_request(self, principal: Principal) -> None:
        if self.request_limit is None:
            return
        accepted = self._reserve_request(
            [self._request_key(principal.api_key_id)],
            [self.request_limit],
        )
        if not accepted:
            raise quota_exceeded("request_quota_exceeded", "Request quota exceeded.")

    def reserve_tokens(self, principal: Principal, estimated_tokens: int) -> TokenReservation:
        reservation = TokenReservation(
            api_key_id=principal.api_key_id,
            amount_tokens=estimated_tokens,
            reservation_id=uuid.uuid4().hex,
        )
        if self.token_limit is None:
            return reservation
        accepted = self._reserve_tokens(
            self._token_keys(principal.api_key_id, reservation.reservation_id),
            [estimated_tokens, self.token_limit],
        )
        if not accepted:
            raise quota_exceeded("token_quota_exceeded", "Token quota exceeded.")
        return reservation

    def reconcile_tokens(self, reservation: TokenReservation, actual_tokens: int) -> None:
        if self.token_limit is None:
            return
        self._reconcile_tokens(
            self._token_keys(reservation.api_key_id, reservation.reservation_id),
            [actual_tokens],
        )

    def release_tokens(self, reservation: TokenReservation) -> None:
        if self.token_limit is None:
            return
        keys = self._token_keys(reservation.api_key_id, reservation.reservation_id)
        self._release_tokens([keys[1], keys[2]], [])

    def _request_key(self, api_key_id: str) -> str:
        return f"{self._namespace}:v1:quota:request:{api_key_id}"

    def _token_keys(self, api_key_id: str, reservation_id: str) -> list[str]:
        prefix = f"{self._namespace}:v1:quota:tokens:{api_key_id}"
        return [
            f"{prefix}:actual",
            f"{prefix}:reserved",
            f"{prefix}:reservation:{reservation_id}",
        ]
