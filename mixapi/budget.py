from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

from mixapi.auth import Principal
from mixapi.errors import budget_exceeded


@dataclass(frozen=True)
class BudgetReservation:
    api_key_id: str
    amount_usd: Decimal
    tracked: bool = False
    reservation_id: str = field(default_factory=lambda: uuid4().hex)


class BudgetService(Protocol):
    def reserve(
        self,
        principal: Principal,
        estimated_cost_usd: Decimal,
        limit_usd: Decimal | None = None,
    ) -> BudgetReservation: ...

    def reconcile(self, reservation: BudgetReservation, actual_cost_usd: Decimal) -> None: ...

    def release(self, reservation: BudgetReservation) -> None: ...


@dataclass
class InMemoryBudgetService:
    limit_usd: Decimal | None = None
    _actual_spend: dict[str, Decimal] = field(
        default_factory=lambda: defaultdict(lambda: Decimal("0"))
    )
    _reserved_spend: dict[str, Decimal] = field(
        default_factory=lambda: defaultdict(lambda: Decimal("0"))
    )

    def reserve(
        self,
        principal: Principal,
        estimated_cost_usd: Decimal,
        limit_usd: Decimal | None = None,
    ) -> BudgetReservation:
        effective_limit = effective_budget_limit(self.limit_usd, limit_usd)
        reservation = BudgetReservation(
            principal.api_key_id,
            estimated_cost_usd,
            tracked=effective_limit is not None,
        )
        if effective_limit is None:
            return reservation

        committed = self._actual_spend[principal.api_key_id]
        reserved = self._reserved_spend[principal.api_key_id]
        if committed + reserved + estimated_cost_usd > effective_limit:
            raise budget_exceeded(
                "api_key_budget_exceeded",
                "API key spend budget exceeded.",
            )

        self._reserved_spend[principal.api_key_id] = reserved + estimated_cost_usd
        return reservation

    def reconcile(self, reservation: BudgetReservation, actual_cost_usd: Decimal) -> None:
        if not reservation.tracked:
            return
        self._reserved_spend[reservation.api_key_id] -= reservation.amount_usd
        self._actual_spend[reservation.api_key_id] += actual_cost_usd

    def release(self, reservation: BudgetReservation) -> None:
        if not reservation.tracked:
            return
        self._reserved_spend[reservation.api_key_id] -= reservation.amount_usd


def effective_budget_limit(
    global_limit_usd: Decimal | None,
    key_limit_usd: Decimal | None,
) -> Decimal | None:
    limits = [limit for limit in (global_limit_usd, key_limit_usd) if limit is not None]
    return min(limits) if limits else None
