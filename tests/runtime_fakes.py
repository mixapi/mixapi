from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from mixapi.budget import BudgetReservation
from mixapi.quota import TokenReservation


class FakeBudgetService:
    def __init__(self, limit_usd: Decimal | None = None) -> None:
        self.limit_usd = limit_usd
        self._reserved_spend = defaultdict(lambda: Decimal("0"))
        self._actual_spend = defaultdict(lambda: Decimal("0"))

    def reserve(self, principal, amount, limit_usd=None):
        reservation = BudgetReservation(principal.api_key_id, amount, tracked=True)
        self._reserved_spend[principal.api_key_id] += amount
        return reservation

    def reconcile(self, reservation, actual):
        self._reserved_spend[reservation.api_key_id] -= reservation.amount_usd
        self._actual_spend[reservation.api_key_id] += actual

    def reconcile_with_usage(self, reservation, actual, usage_ledger, events):
        self.reconcile(reservation, actual)
        return [usage_ledger.record(event) for event in events]

    def release(self, reservation):
        self._reserved_spend[reservation.api_key_id] -= reservation.amount_usd


class FakeQuotaService:
    def __init__(self, token_limit=None) -> None:
        self.token_limit = token_limit
        self._actual_tokens = defaultdict(int)
        self._token_reservations = {}

    def reserve_tokens(self, principal, amount):
        reservation = TokenReservation(principal.api_key_id, amount)
        self._token_reservations[reservation.reservation_id] = reservation
        return reservation

    def reconcile_tokens(self, reservation, actual):
        self._token_reservations.pop(reservation.reservation_id, None)
        self._actual_tokens[reservation.api_key_id] += actual

    def release_tokens(self, reservation):
        self._token_reservations.pop(reservation.reservation_id, None)


class FakeCircuitBreaker:
    def __init__(self, *args, now=None, recovery_timeout_seconds=30, **kwargs) -> None:
        self.now = now or (lambda: 0)
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.opened_at = None

    def is_open(self, provider, provider_model):
        if self.opened_at is None:
            return False
        if self.now() - self.opened_at >= self.recovery_timeout_seconds:
            self.opened_at = None
            return False
        return True

    def record_failure(self, provider, provider_model, reason):
        self.opened_at = self.now()

    def record_success(self, provider, provider_model):
        self.opened_at = None
