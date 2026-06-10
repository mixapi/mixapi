from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from psycopg.types.json import Jsonb

from mixapi.auth import Principal
from mixapi.budget import BudgetReservation, effective_budget_limit
from mixapi.errors import budget_exceeded
from mixapi.postgres import PostgresPool
from mixapi.repositories.usage import PostgresUsageLedger
from mixapi.usage import UsageEvent


class PostgresBudgetService:
    def __init__(self, pool: PostgresPool, limit_usd: Decimal | None = None) -> None:
        self._pool = pool
        self.limit_usd = limit_usd

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
        )
        if effective_limit is None:
            return reservation
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO budget_spend (api_key_id, actual_spend_usd)
                VALUES (%s, 0) ON CONFLICT (api_key_id) DO NOTHING
                """,
                (principal.api_key_id,),
            )
            spend_row = connection.execute(
                """
                SELECT actual_spend_usd FROM budget_spend
                WHERE api_key_id = %s FOR UPDATE
                """,
                (principal.api_key_id,),
            ).fetchone()
            reserved_row = connection.execute(
                """
                SELECT COALESCE(
                    sum((reservation_data->>'amount_usd')::numeric), 0
                ) AS reserved_spend
                FROM usage_write_intents
                WHERE api_key_id = %s AND endpoint = 'budget-reservation'
                  AND status = 'pending'
                """,
                (principal.api_key_id,),
            ).fetchone()
            if (
                spend_row["actual_spend_usd"]
                + reserved_row["reserved_spend"]
                + estimated_cost_usd
                > effective_limit
            ):
                raise budget_exceeded(
                    "api_key_budget_exceeded",
                    "API key spend budget exceeded.",
                )
            intent_id = _budget_intent_id(reservation.reservation_id)
            connection.execute(
                """
                INSERT INTO usage_write_intents (
                    id, request_id, tenant_id, project_id, api_key_id,
                    endpoint, logical_model, configuration_version,
                    reservation_data
                ) VALUES (%s, %s, %s, %s, %s, 'budget-reservation', '', 0, %s)
                """,
                (
                    intent_id,
                    intent_id,
                    principal.tenant_id,
                    principal.project_id,
                    principal.api_key_id,
                    Jsonb(
                        {
                            "amount_usd": str(estimated_cost_usd),
                            "limit_usd": str(effective_limit),
                            "reservation_id": reservation.reservation_id,
                        }
                    ),
                ),
            )
        return reservation

    def reconcile(self, reservation: BudgetReservation, actual_cost_usd: Decimal) -> None:
        PostgresUsageLedger(self._pool).settle_reservation(
            reservation_id=reservation.reservation_id,
            api_key_id=reservation.api_key_id,
            actual_cost_usd=actual_cost_usd,
            events=(),
            tracked=reservation.tracked,
        )

    def reconcile_with_usage(
        self,
        reservation: BudgetReservation,
        actual_cost_usd: Decimal,
        usage_ledger: PostgresUsageLedger,
        events: Iterable[UsageEvent],
    ) -> list[UsageEvent]:
        return usage_ledger.settle_reservation(
            reservation_id=reservation.reservation_id,
            api_key_id=reservation.api_key_id,
            actual_cost_usd=actual_cost_usd,
            events=events,
            tracked=reservation.tracked,
        )

    def release(self, reservation: BudgetReservation) -> None:
        if not reservation.tracked:
            return
        with self._pool.connection() as connection:
            connection.execute(
                """
                UPDATE usage_write_intents
                SET status = 'failed', updated_at = now(), completed_at = now()
                WHERE id = %s AND api_key_id = %s AND status = 'pending'
                """,
                (_budget_intent_id(reservation.reservation_id), reservation.api_key_id),
            )

    def actual_spend(self, api_key_id: str) -> Decimal:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT actual_spend_usd FROM budget_spend WHERE api_key_id = %s",
                (api_key_id,),
            ).fetchone()
        return row["actual_spend_usd"] if row is not None else Decimal("0")

    def add_settled_spend(self, api_key_id: str, amount_usd: Decimal) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO budget_spend (api_key_id, actual_spend_usd)
                VALUES (%s, %s)
                ON CONFLICT (api_key_id) DO UPDATE SET
                    actual_spend_usd = budget_spend.actual_spend_usd
                        + EXCLUDED.actual_spend_usd,
                    updated_at = now()
                """,
                (api_key_id, amount_usd),
            )

    def mark_reconciliation_processed(self, reservation_id: str) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                UPDATE budget_reconciliation_outbox
                SET processed_at = now(), claim_expires_at = NULL
                WHERE reservation_id = %s AND processed_at IS NULL
                """,
                (reservation_id,),
            )


def _budget_intent_id(reservation_id: str) -> str:
    return f"budget_{reservation_id}"
