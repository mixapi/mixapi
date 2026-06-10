from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4

from mixapi.auth import Principal


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

    def reconcile_with_usage(
        self,
        reservation: BudgetReservation,
        actual_cost_usd: Decimal,
        usage_ledger: Any,
        events: list[Any],
    ) -> list[Any]: ...

    def release(self, reservation: BudgetReservation) -> None: ...


def effective_budget_limit(
    global_limit_usd: Decimal | None,
    key_limit_usd: Decimal | None,
) -> Decimal | None:
    limits = [limit for limit in (global_limit_usd, key_limit_usd) if limit is not None]
    return min(limits) if limits else None
