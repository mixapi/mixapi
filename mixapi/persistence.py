from __future__ import annotations

import sqlite3
import json
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from time import time
from typing import Callable, Iterator

from mixapi.usage import UsageEvent
from mixapi.auth import Principal
from mixapi.budget import BudgetReservation
from mixapi.circuits import is_transient_failure
from mixapi.errors import budget_exceeded, not_found, validation_error
from mixapi.idempotency import IdempotencyReplay, request_hash
from mixapi.route_decisions import RouteDecisionRecord


class SQLiteDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    @contextmanager
    def connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize_schema(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    api_key_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    logical_model TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    provider_model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_usd TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_events_tenant ON usage_events(tenant_id, id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_records (
                    tenant_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, endpoint, idempotency_key)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS route_decisions (
                    tenant_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    logical_model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    selected_provider TEXT,
                    selected_provider_model TEXT,
                    attempts_json TEXT NOT NULL,
                    rejected_candidates_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, request_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS budget_spend (
                    api_key_id TEXT PRIMARY KEY,
                    actual_spend_usd TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS budget_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    api_key_id TEXT NOT NULL,
                    amount_usd TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_budget_reservations_api_key
                ON budget_reservations(api_key_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS circuit_states (
                    provider TEXT NOT NULL,
                    provider_model TEXT NOT NULL,
                    consecutive_failures INTEGER NOT NULL,
                    opened_at REAL,
                    PRIMARY KEY (provider, provider_model)
                )
                """
            )


class SQLiteBudgetService:
    def __init__(self, database: SQLiteDatabase, limit_usd: Decimal | None = None) -> None:
        self.database = database
        self.limit_usd = limit_usd

    def reserve(self, principal: Principal, estimated_cost_usd: Decimal) -> BudgetReservation:
        reservation = BudgetReservation(principal.api_key_id, estimated_cost_usd)
        if self.limit_usd is None:
            return reservation

        denied = False
        with self.database.connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT actual_spend_usd FROM budget_spend WHERE api_key_id = ?",
                (principal.api_key_id,),
            ).fetchone()
            actual_spend = Decimal(row["actual_spend_usd"]) if row else Decimal("0")
            reservation_rows = connection.execute(
                "SELECT amount_usd FROM budget_reservations WHERE api_key_id = ?",
                (principal.api_key_id,),
            ).fetchall()
            reserved_spend = sum(
                (Decimal(item["amount_usd"]) for item in reservation_rows),
                start=Decimal("0"),
            )
            if actual_spend + reserved_spend + estimated_cost_usd > self.limit_usd:
                denied = True
            else:
                connection.execute(
                    """
                    INSERT INTO budget_reservations (reservation_id, api_key_id, amount_usd)
                    VALUES (?, ?, ?)
                    """,
                    (
                        reservation.reservation_id,
                        reservation.api_key_id,
                        str(reservation.amount_usd),
                    ),
                )
        if denied:
            raise budget_exceeded(
                "api_key_budget_exceeded",
                "API key spend budget exceeded.",
            )
        return reservation

    def reconcile(self, reservation: BudgetReservation, actual_cost_usd: Decimal) -> None:
        if self.limit_usd is None:
            return

        with self.database.connect(immediate=True) as connection:
            stored = connection.execute(
                """
                SELECT reservation_id FROM budget_reservations
                WHERE reservation_id = ? AND api_key_id = ?
                """,
                (reservation.reservation_id, reservation.api_key_id),
            ).fetchone()
            if stored is None:
                return
            connection.execute(
                "DELETE FROM budget_reservations WHERE reservation_id = ?",
                (reservation.reservation_id,),
            )
            row = connection.execute(
                "SELECT actual_spend_usd FROM budget_spend WHERE api_key_id = ?",
                (reservation.api_key_id,),
            ).fetchone()
            actual_spend = Decimal(row["actual_spend_usd"]) if row else Decimal("0")
            connection.execute(
                """
                INSERT INTO budget_spend (api_key_id, actual_spend_usd)
                VALUES (?, ?)
                ON CONFLICT(api_key_id) DO UPDATE SET
                    actual_spend_usd = excluded.actual_spend_usd
                """,
                (reservation.api_key_id, str(actual_spend + actual_cost_usd)),
            )

    def release(self, reservation: BudgetReservation) -> None:
        if self.limit_usd is None:
            return

        with self.database.connect(immediate=True) as connection:
            connection.execute(
                """
                DELETE FROM budget_reservations
                WHERE reservation_id = ? AND api_key_id = ?
                """,
                (reservation.reservation_id, reservation.api_key_id),
            )


class SQLiteCircuitBreaker:
    def __init__(
        self,
        database: SQLiteDatabase,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 30.0,
        now: Callable[[], float] = time,
    ) -> None:
        self.database = database
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.now = now

    def is_open(self, provider: str, provider_model: str) -> bool:
        with self.database.connect() as connection:
            state = connection.execute(
                """
                SELECT opened_at FROM circuit_states
                WHERE provider = ? AND provider_model = ?
                """,
                (provider, provider_model),
            ).fetchone()
            if state is None or state["opened_at"] is None:
                return False
            if self.now() - state["opened_at"] < self.recovery_timeout_seconds:
                return True

        with self.database.connect(immediate=True) as connection:
            state = connection.execute(
                """
                SELECT opened_at FROM circuit_states
                WHERE provider = ? AND provider_model = ?
                """,
                (provider, provider_model),
            ).fetchone()
            if state is None or state["opened_at"] is None:
                return False
            if self.now() - state["opened_at"] < self.recovery_timeout_seconds:
                return True
            connection.execute(
                """
                DELETE FROM circuit_states
                WHERE provider = ? AND provider_model = ?
                """,
                (provider, provider_model),
            )
            return False

    def record_failure(self, provider: str, provider_model: str, reason: str) -> None:
        if not is_transient_failure(reason):
            return
        with self.database.connect(immediate=True) as connection:
            state = connection.execute(
                """
                SELECT consecutive_failures, opened_at FROM circuit_states
                WHERE provider = ? AND provider_model = ?
                """,
                (provider, provider_model),
            ).fetchone()
            consecutive_failures = (
                state["consecutive_failures"] + 1 if state is not None else 1
            )
            opened_at = state["opened_at"] if state is not None else None
            if opened_at is None and consecutive_failures >= self.failure_threshold:
                opened_at = self.now()
            connection.execute(
                """
                INSERT INTO circuit_states (
                    provider, provider_model, consecutive_failures, opened_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(provider, provider_model) DO UPDATE SET
                    consecutive_failures = excluded.consecutive_failures,
                    opened_at = excluded.opened_at
                """,
                (provider, provider_model, consecutive_failures, opened_at),
            )

    def record_success(self, provider: str, provider_model: str) -> None:
        with self.database.connect(immediate=True) as connection:
            connection.execute(
                """
                DELETE FROM circuit_states
                WHERE provider = ? AND provider_model = ?
                """,
                (provider, provider_model),
            )


class SQLiteUsageLedger:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def record(self, event: UsageEvent) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO usage_events (
                    request_id, tenant_id, project_id, api_key_id, endpoint,
                    logical_model, provider, provider_model, input_tokens,
                    output_tokens, cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.request_id,
                    event.tenant_id,
                    event.project_id,
                    event.api_key_id,
                    event.endpoint,
                    event.logical_model,
                    event.provider,
                    event.provider_model,
                    event.input_tokens,
                    event.output_tokens,
                    str(event.cost_usd),
                ),
            )

    def events(self, tenant_id: str | None = None) -> list[UsageEvent]:
        query = "SELECT * FROM usage_events"
        parameters: tuple[str, ...] = ()
        if tenant_id is not None:
            query += " WHERE tenant_id = ?"
            parameters = (tenant_id,)
        query += " ORDER BY id"

        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()

        return [
            UsageEvent(
                request_id=row["request_id"],
                tenant_id=row["tenant_id"],
                project_id=row["project_id"],
                api_key_id=row["api_key_id"],
                endpoint=row["endpoint"],
                logical_model=row["logical_model"],
                provider=row["provider"],
                provider_model=row["provider_model"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                cost_usd=Decimal(row["cost_usd"]),
            )
            for row in rows
        ]


class SQLiteIdempotencyStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def replay(
        self,
        principal: Principal,
        endpoint: str,
        key: str | None,
        request_body: dict,
    ) -> IdempotencyReplay | None:
        if not key:
            return None

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT request_hash, response_json
                FROM idempotency_records
                WHERE tenant_id = ? AND endpoint = ? AND idempotency_key = ?
                """,
                (principal.tenant_id, endpoint, key),
            ).fetchone()

        if row is None:
            return None
        if row["request_hash"] != request_hash(request_body):
            raise validation_error(
                "idempotency_key_reused",
                "Idempotency key was reused with a different request body.",
            )
        return IdempotencyReplay(response=json.loads(row["response_json"]))

    def store(
        self,
        principal: Principal,
        endpoint: str,
        key: str | None,
        request_body: dict,
        response: dict,
    ) -> None:
        if not key:
            return

        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO idempotency_records (
                    tenant_id, endpoint, idempotency_key, request_hash, response_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, endpoint, idempotency_key) DO NOTHING
                """,
                (
                    principal.tenant_id,
                    endpoint,
                    key,
                    request_hash(request_body),
                    json.dumps(response, sort_keys=True, separators=(",", ":")),
                ),
            )


class SQLiteRouteDecisionStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def record(self, record: RouteDecisionRecord) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO route_decisions (
                    tenant_id, request_id, project_id, endpoint, logical_model,
                    status, selected_provider, selected_provider_model,
                    attempts_json, rejected_candidates_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, request_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    endpoint = excluded.endpoint,
                    logical_model = excluded.logical_model,
                    status = excluded.status,
                    selected_provider = excluded.selected_provider,
                    selected_provider_model = excluded.selected_provider_model,
                    attempts_json = excluded.attempts_json,
                    rejected_candidates_json = excluded.rejected_candidates_json
                """,
                (
                    record.tenant_id,
                    record.request_id,
                    record.project_id,
                    record.endpoint,
                    record.logical_model,
                    record.status,
                    record.selected_provider,
                    record.selected_provider_model,
                    json.dumps(record.attempts, separators=(",", ":")),
                    json.dumps(record.rejected_candidates, separators=(",", ":")),
                ),
            )

    def get_public(self, request_id: str, tenant_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM route_decisions
                WHERE tenant_id = ? AND request_id = ?
                """,
                (tenant_id, request_id),
            ).fetchone()

        if row is None:
            raise not_found("route_decision_not_found", "Route decision was not found.")

        return RouteDecisionRecord(
            request_id=row["request_id"],
            tenant_id=row["tenant_id"],
            project_id=row["project_id"],
            endpoint=row["endpoint"],
            logical_model=row["logical_model"],
            status=row["status"],
            selected_provider=row["selected_provider"],
            selected_provider_model=row["selected_provider_model"],
            attempts=tuple(json.loads(row["attempts_json"])),
            rejected_candidates=tuple(json.loads(row["rejected_candidates_json"])),
        ).public_dict()
