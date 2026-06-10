from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal
from typing import Iterable

import redis

from mixapi.auth import Principal
from mixapi.budget import BudgetReservation, effective_budget_limit
from mixapi.errors import budget_exceeded
from mixapi.redis_scripts import LuaScript
from mixapi.repositories.budgets import PostgresBudgetService
from mixapi.repositories.usage import PostgresUsageLedger
from mixapi.usage import UsageEvent


_RESERVE = """
redis.call('SETNX', KEYS[1], ARGV[1])
local actual = tonumber(redis.call('GET', KEYS[1]) or '0')
local reserved = tonumber(redis.call('GET', KEYS[2]) or '0')
local amount = tonumber(ARGV[2])
if actual + reserved + amount > tonumber(ARGV[3]) then return 0 end
redis.call('SET', KEYS[3], amount)
redis.call('INCRBY', KEYS[2], amount)
return 1
"""

_RECONCILE = """
if redis.call('EXISTS', KEYS[4]) == 1 then return 0 end
local amount = redis.call('GET', KEYS[3])
if amount then
  redis.call('DECRBY', KEYS[2], tonumber(amount))
  redis.call('DEL', KEYS[3])
end
redis.call('SET', KEYS[1], ARGV[1])
redis.call('SET', KEYS[4], '1', 'EX', ARGV[2])
return 1
"""

_RELEASE = """
local amount = redis.call('GET', KEYS[2])
if not amount then return 0 end
redis.call('DECRBY', KEYS[1], tonumber(amount))
redis.call('DEL', KEYS[2])
return 1
"""


class RedisBudgetService:
    def __init__(
        self,
        client: redis.Redis,
        durable: PostgresBudgetService,
        *,
        namespace: str,
        limit_usd: Decimal | None = None,
        reconciliation_ttl_seconds: int = 604_800,
    ) -> None:
        self._namespace = namespace
        self._durable = durable
        self.limit_usd = limit_usd
        self._reconciliation_ttl_seconds = reconciliation_ttl_seconds
        self._reserve = LuaScript(client, _RESERVE)
        self._reconcile = LuaScript(client, _RECONCILE)
        self._release = LuaScript(client, _RELEASE)

    def reserve(
        self,
        principal: Principal,
        estimated_cost_usd: Decimal,
        limit_usd: Decimal | None = None,
    ) -> BudgetReservation:
        effective_limit = effective_budget_limit(self.limit_usd, limit_usd)
        reservation = BudgetReservation(
            api_key_id=principal.api_key_id,
            amount_usd=estimated_cost_usd,
            tracked=effective_limit is not None,
            reservation_id=uuid.uuid4().hex,
        )
        if effective_limit is None:
            return reservation
        accepted = self._reserve(
            self._keys(principal.api_key_id, reservation.reservation_id)[:3],
            [
                _units(self._durable.actual_spend(principal.api_key_id)),
                _units(estimated_cost_usd),
                _units(effective_limit),
            ],
        )
        if not accepted:
            raise budget_exceeded(
                "api_key_budget_exceeded",
                "API key spend budget exceeded.",
            )
        return reservation

    def reconcile(self, reservation: BudgetReservation, actual_cost_usd: Decimal) -> None:
        durable_reservation = replace(reservation, tracked=False)
        self._durable.reconcile(durable_reservation, actual_cost_usd)
        self._finish_redis_reconciliation(reservation)

    def reconcile_with_usage(
        self,
        reservation: BudgetReservation,
        actual_cost_usd: Decimal,
        usage_ledger: PostgresUsageLedger,
        events: Iterable[UsageEvent],
    ) -> list[UsageEvent]:
        recorded = self._durable.reconcile_with_usage(
            replace(reservation, tracked=False),
            actual_cost_usd,
            usage_ledger,
            events,
        )
        self._finish_redis_reconciliation(reservation)
        return recorded

    def release(self, reservation: BudgetReservation) -> None:
        if not reservation.tracked:
            return
        keys = self._keys(reservation.api_key_id, reservation.reservation_id)
        self._release([keys[1], keys[2]], [])

    def _finish_redis_reconciliation(self, reservation: BudgetReservation) -> None:
        if reservation.tracked:
            actual = self._durable.actual_spend(reservation.api_key_id)
            self._reconcile(
                self._keys(reservation.api_key_id, reservation.reservation_id),
                [_units(actual), self._reconciliation_ttl_seconds],
            )
        self._durable.mark_reconciliation_processed(reservation.reservation_id)

    def _keys(self, api_key_id: str, reservation_id: str) -> list[str]:
        prefix = f"{self._namespace}:v1:budget:{api_key_id}"
        return [
            f"{prefix}:actual",
            f"{prefix}:reserved",
            f"{prefix}:reservation:{reservation_id}",
            f"{prefix}:reconciled:{reservation_id}",
        ]


def _units(value: Decimal) -> int:
    return int(value * Decimal("100000000"))
