from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from typing import Any, Iterable

from psycopg import Connection
from psycopg.types.json import Jsonb

from mixapi.postgres import PostgresPool
from mixapi.usage import UsageEvent, UsagePage, UsageQuery, UsageWriteIntent


class PostgresUsageLedger:
    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def begin_intent(self, intent: UsageWriteIntent) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO usage_write_intents (
                    id, request_id, tenant_id, project_id, api_key_id,
                    endpoint, logical_model, configuration_version,
                    reservation_data
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (request_id) DO NOTHING
                """,
                (
                    _intent_id(intent.request_id),
                    intent.request_id,
                    intent.tenant_id,
                    intent.project_id,
                    intent.api_key_id,
                    intent.endpoint,
                    intent.logical_model,
                    intent.configuration_version,
                    Jsonb(intent.reservation_data),
                ),
            )

    def fail_intent(self, request_id: str) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                UPDATE usage_write_intents
                SET status = 'failed', updated_at = now(), completed_at = now()
                WHERE request_id = %s AND status = 'pending'
                """,
                (request_id,),
            )

    def record(self, event: UsageEvent) -> UsageEvent:
        recorded = self.settle_reservation(
            reservation_id=f"usage_{uuid.uuid4().hex}",
            api_key_id=event.api_key_id,
            actual_cost_usd=event.cost_usd,
            events=(event,),
            tracked=False,
        )
        return recorded[0]

    def settle_reservation(
        self,
        *,
        reservation_id: str,
        api_key_id: str,
        actual_cost_usd: Decimal,
        events: Iterable[UsageEvent],
        tracked: bool,
    ) -> list[UsageEvent]:
        event_values = tuple(events)
        with self._pool.connection() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (reservation_id,),
            )
            if tracked:
                claimed = connection.execute(
                    """
                    UPDATE usage_write_intents
                    SET status = 'completed', updated_at = now(), completed_at = now()
                    WHERE id = %s AND api_key_id = %s AND status = 'pending'
                    RETURNING id
                    """,
                    (_budget_intent_id(reservation_id), api_key_id),
                ).fetchone()
                if claimed is None:
                    if self._settlement_exists(connection, reservation_id):
                        return []
                    raise ValueError("budget reservation is missing or already finalized")
            elif self._settlement_exists(connection, reservation_id):
                return []

            recorded = [self._insert_event(connection, event) for event in event_values]
            connection.execute(
                """
                INSERT INTO budget_spend (api_key_id, actual_spend_usd)
                VALUES (%s, %s)
                ON CONFLICT (api_key_id) DO UPDATE SET
                    actual_spend_usd = budget_spend.actual_spend_usd
                        + EXCLUDED.actual_spend_usd,
                    updated_at = now()
                """,
                (api_key_id, actual_cost_usd),
            )
            connection.execute(
                """
                INSERT INTO budget_reconciliation_outbox (
                    reservation_id, api_key_id, amount_usd
                ) VALUES (%s, %s, %s)
                """,
                (reservation_id, api_key_id, actual_cost_usd),
            )
            for request_id in {event.request_id for event in event_values}:
                connection.execute(
                    """
                    UPDATE usage_write_intents
                    SET status = 'completed', updated_at = now(), completed_at = now()
                    WHERE request_id = %s AND endpoint <> 'budget-reservation'
                      AND status = 'pending'
                    """,
                    (request_id,),
                )
            return recorded

    def events(self, tenant_id: str | None = None) -> list[UsageEvent]:
        with self._pool.connection() as connection:
            if tenant_id is None:
                rows = connection.execute(
                    "SELECT * FROM usage_events ORDER BY created_at, id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM usage_events
                    WHERE tenant_id = %s ORDER BY created_at, id
                    """,
                    (tenant_id,),
                ).fetchall()
        return [_event_from_row(row) for row in rows]

    def query(self, query: UsageQuery) -> UsagePage:
        clauses = ["tenant_id = %s"]
        parameters: list[Any] = [query.tenant_id]
        if query.start_time is not None:
            clauses.append("created_at >= %s")
            parameters.append(query.start_time)
        if query.end_time is not None:
            clauses.append("created_at <= %s")
            parameters.append(query.end_time)
        if query.after_created_at is not None:
            clauses.append("(created_at, id) > (%s, %s)")
            parameters.extend((query.after_created_at, query.after_sequence_id))
        elif query.after_sequence_id > 0:
            clauses.append("id > %s")
            parameters.append(query.after_sequence_id)
        fetch_limit = query.limit + 1 if query.limit is not None else None
        limit_sql = ""
        if fetch_limit is not None:
            limit_sql = " LIMIT %s"
            parameters.append(fetch_limit)
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM usage_events
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at, id{limit_sql}
                """,
                parameters,
            ).fetchall()
        has_more = query.limit is not None and len(rows) > query.limit
        if has_more:
            rows = rows[: query.limit]
        return UsagePage(events=[_event_from_row(row) for row in rows], has_more=has_more)

    @staticmethod
    def _insert_event(connection: Connection, event: UsageEvent) -> UsageEvent:
        row = connection.execute(
            """
            INSERT INTO usage_events (
                request_id, tenant_id, project_id, api_key_id, endpoint,
                logical_model, provider_connection_id, provider_protocol,
                provider_model, configuration_version, input_tokens,
                output_tokens, cost_usd, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                event.request_id,
                event.tenant_id,
                event.project_id,
                event.api_key_id,
                event.endpoint,
                event.logical_model,
                event.provider_connection_id or event.provider,
                event.provider,
                event.provider_model,
                event.configuration_version,
                event.input_tokens,
                event.output_tokens,
                event.cost_usd,
                event.created_at,
            ),
        ).fetchone()
        return _event_from_row(row)

    @staticmethod
    def _settlement_exists(connection: Connection, reservation_id: str) -> bool:
        return connection.execute(
            """
            SELECT 1 FROM budget_reconciliation_outbox
            WHERE reservation_id = %s
            """,
            (reservation_id,),
        ).fetchone() is not None


def _event_from_row(row: dict[str, Any]) -> UsageEvent:
    return UsageEvent(
        request_id=row["request_id"],
        tenant_id=row["tenant_id"],
        project_id=row["project_id"],
        api_key_id=row["api_key_id"],
        endpoint=row["endpoint"],
        logical_model=row["logical_model"],
        provider=row["provider_protocol"],
        provider_model=row["provider_model"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cost_usd=row["cost_usd"],
        created_at=row["created_at"],
        sequence_id=row["id"],
        provider_connection_id=row["provider_connection_id"],
        configuration_version=row["configuration_version"],
    )


def _intent_id(request_id: str) -> str:
    return f"usage_{hashlib.sha256(request_id.encode('utf-8')).hexdigest()[:64]}"


def _budget_intent_id(reservation_id: str) -> str:
    return f"budget_{reservation_id}"
