from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from time import time
from typing import Any
from typing import Callable, Iterator
from uuid import uuid4

from mixapi.auth import Principal
from mixapi.budget import BudgetReservation
from mixapi.circuits import is_transient_failure
from mixapi.control_plane import (
    ApiKeyRecord,
    AuditEvent,
    CreatedApiKey,
    TenantPolicy,
    generate_api_key,
    hash_api_key,
)
from mixapi.errors import budget_exceeded, not_found, validation_error
from mixapi.idempotency import IdempotencyReplay, request_hash
from mixapi.route_decisions import RouteDecisionRecord
from mixapi.usage import (
    UsageEvent,
    UsagePage,
    UsageQuery,
    format_usage_timestamp,
    parse_usage_timestamp,
)


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
                    cost_usd TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            usage_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(usage_events)")
            }
            if "created_at" not in usage_columns:
                connection.execute("ALTER TABLE usage_events ADD COLUMN created_at TEXT")
                connection.execute(
                    "UPDATE usage_events SET created_at = ? WHERE created_at IS NULL",
                    (format_usage_timestamp(datetime.now(timezone.utc)),),
                )
            noncanonical_rows = connection.execute(
                """
                SELECT id, created_at FROM usage_events
                WHERE created_at IS NULL OR length(created_at) != 27
                """
            ).fetchall()
            for row in noncanonical_rows:
                created_at = parse_usage_timestamp(row["created_at"])
                if created_at is None:
                    created_at = datetime.now(timezone.utc)
                connection.execute(
                    "UPDATE usage_events SET created_at = ? WHERE id = ?",
                    (format_usage_timestamp(created_at), row["id"]),
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_events_tenant ON usage_events(tenant_id, id)"
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_usage_events_tenant_created_at_id
                ON usage_events(tenant_id, created_at, id)
                """
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    name TEXT,
                    key_hash TEXT NOT NULL UNIQUE,
                    key_prefix TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    model_allowlist_json TEXT NOT NULL,
                    budget_limit_usd TEXT,
                    expires_at TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys(tenant_id, created_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant_policies (
                    tenant_id TEXT PRIMARY KEY,
                    model_allowlist_json TEXT NOT NULL,
                    routing_objective TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    actor_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    before_json TEXT,
                    after_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_events_tenant
                ON audit_events(tenant_id, sequence_id)
                """
            )


class SQLiteControlPlaneStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def create_api_key(
        self,
        *,
        tenant_id: str,
        project_id: str,
        name: str | None,
        scopes: tuple[str, ...],
        model_allowlist: tuple[str, ...],
        budget_limit_usd: Decimal | None,
        expires_at: datetime | None,
        actor_id: str,
    ) -> CreatedApiKey:
        secret = generate_api_key()
        now = datetime.now(timezone.utc)
        record = ApiKeyRecord(
            id=f"key_{uuid4().hex}",
            tenant_id=tenant_id,
            project_id=project_id,
            name=name,
            key_hash=hash_api_key(secret),
            key_prefix=secret[:12],
            scopes=tuple(scopes),
            model_allowlist=tuple(model_allowlist),
            budget_limit_usd=budget_limit_usd,
            expires_at=expires_at,
            status="active",
            created_at=now,
            updated_at=now,
        )
        with self.database.connect(immediate=True) as connection:
            self._write_api_key(connection, record, insert=True)
            self._insert_audit(
                connection,
                actor_id=actor_id,
                tenant_id=tenant_id,
                action="api_key.created",
                target_type="api_key",
                target_id=record.id,
                before=None,
                after=record.public_dict(),
                created_at=now,
            )
        return CreatedApiKey(record=record, secret=secret)

    def resolve_api_key(self, secret: str) -> ApiKeyRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM api_keys WHERE key_hash = ?",
                (hash_api_key(secret),),
            ).fetchone()
        if row is None:
            return None
        record = _api_key_from_row(row)
        if record.status != "active":
            return None
        if record.expires_at is not None and record.expires_at <= datetime.now(timezone.utc):
            return None
        return record

    def get_api_key(self, api_key_id: str) -> ApiKeyRecord:
        with self.database.connect() as connection:
            record = self._get_api_key(connection, api_key_id)
        if record is None:
            raise not_found("api_key_not_found", "API key was not found.")
        return record

    def list_api_keys(self, tenant_id: str | None = None) -> list[ApiKeyRecord]:
        statement = "SELECT * FROM api_keys"
        parameters: tuple[str, ...] = ()
        if tenant_id is not None:
            statement += " WHERE tenant_id = ?"
            parameters = (tenant_id,)
        statement += " ORDER BY created_at, id"
        with self.database.connect() as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [_api_key_from_row(row) for row in rows]

    def update_api_key(
        self,
        api_key_id: str,
        changes: dict[str, Any],
        *,
        actor_id: str,
    ) -> ApiKeyRecord:
        with self.database.connect(immediate=True) as connection:
            record = self._get_api_key(connection, api_key_id)
            if record is None:
                raise not_found("api_key_not_found", "API key was not found.")
            updated = replace(record, **changes, updated_at=datetime.now(timezone.utc))
            self._write_api_key(connection, updated, insert=False)
            self._insert_audit(
                connection,
                actor_id=actor_id,
                tenant_id=updated.tenant_id,
                action="api_key.updated",
                target_type="api_key",
                target_id=updated.id,
                before=record.public_dict(),
                after=updated.public_dict(),
                created_at=updated.updated_at,
            )
            return updated

    def revoke_api_key(self, api_key_id: str, *, actor_id: str) -> ApiKeyRecord:
        with self.database.connect(immediate=True) as connection:
            record = self._get_api_key(connection, api_key_id)
            if record is None:
                raise not_found("api_key_not_found", "API key was not found.")
            now = datetime.now(timezone.utc)
            revoked = replace(record, status="revoked", updated_at=now)
            self._write_api_key(connection, revoked, insert=False)
            self._insert_audit(
                connection,
                actor_id=actor_id,
                tenant_id=revoked.tenant_id,
                action="api_key.revoked",
                target_type="api_key",
                target_id=revoked.id,
                before=record.public_dict(),
                after=revoked.public_dict(),
                created_at=now,
            )
            return revoked

    def set_tenant_model_allowlist(
        self,
        tenant_id: str,
        model_allowlist: tuple[str, ...],
        *,
        actor_id: str,
    ) -> TenantPolicy:
        return self._update_tenant_policy(
            tenant_id,
            model_allowlist=tuple(model_allowlist),
            routing_objective=None,
            update_objective=False,
            actor_id=actor_id,
            action="tenant.model_allowlist.updated",
        )

    def set_tenant_routing_policy(
        self,
        tenant_id: str,
        objective: str | None,
        *,
        actor_id: str,
    ) -> TenantPolicy:
        return self._update_tenant_policy(
            tenant_id,
            model_allowlist=None,
            routing_objective=objective,
            update_objective=True,
            actor_id=actor_id,
            action="tenant.routing_policy.updated",
        )

    def get_tenant_policy(self, tenant_id: str) -> TenantPolicy:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tenant_policies WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        return _tenant_policy_from_row(row) if row is not None else TenantPolicy(tenant_id)

    def list_audit_events(self, tenant_id: str | None = None) -> list[AuditEvent]:
        statement = "SELECT * FROM audit_events"
        parameters: tuple[str, ...] = ()
        if tenant_id is not None:
            statement += " WHERE tenant_id = ?"
            parameters = (tenant_id,)
        statement += " ORDER BY sequence_id"
        with self.database.connect() as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [_audit_event_from_row(row) for row in rows]

    def _update_tenant_policy(
        self,
        tenant_id: str,
        *,
        model_allowlist: tuple[str, ...] | None,
        routing_objective: str | None,
        update_objective: bool,
        actor_id: str,
        action: str,
    ) -> TenantPolicy:
        with self.database.connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM tenant_policies WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            current = _tenant_policy_from_row(row) if row is not None else TenantPolicy(tenant_id)
            now = datetime.now(timezone.utc)
            updated = replace(
                current,
                model_allowlist=(
                    model_allowlist if model_allowlist is not None else current.model_allowlist
                ),
                routing_objective=(
                    routing_objective if update_objective else current.routing_objective
                ),
                updated_at=now,
            )
            connection.execute(
                """
                INSERT INTO tenant_policies (
                    tenant_id, model_allowlist_json, routing_objective, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET
                    model_allowlist_json = excluded.model_allowlist_json,
                    routing_objective = excluded.routing_objective,
                    updated_at = excluded.updated_at
                """,
                (
                    updated.tenant_id,
                    json.dumps(updated.model_allowlist, separators=(",", ":")),
                    updated.routing_objective,
                    _format_control_datetime(updated.updated_at),
                ),
            )
            self._insert_audit(
                connection,
                actor_id=actor_id,
                tenant_id=tenant_id,
                action=action,
                target_type="tenant_policy",
                target_id=tenant_id,
                before=current.public_dict(),
                after=updated.public_dict(),
                created_at=now,
            )
            return updated

    def _get_api_key(
        self,
        connection: sqlite3.Connection,
        api_key_id: str,
    ) -> ApiKeyRecord | None:
        row = connection.execute(
            "SELECT * FROM api_keys WHERE id = ?",
            (api_key_id,),
        ).fetchone()
        return _api_key_from_row(row) if row is not None else None

    def _write_api_key(
        self,
        connection: sqlite3.Connection,
        record: ApiKeyRecord,
        *,
        insert: bool,
    ) -> None:
        values = (
            record.tenant_id,
            record.project_id,
            record.name,
            record.key_hash,
            record.key_prefix,
            json.dumps(record.scopes, separators=(",", ":")),
            json.dumps(record.model_allowlist, separators=(",", ":")),
            str(record.budget_limit_usd) if record.budget_limit_usd is not None else None,
            _format_control_datetime(record.expires_at),
            record.status,
            _format_control_datetime(record.created_at),
            _format_control_datetime(record.updated_at),
        )
        if insert:
            connection.execute(
                """
                INSERT INTO api_keys (
                    id, tenant_id, project_id, name, key_hash, key_prefix,
                    scopes_json, model_allowlist_json, budget_limit_usd,
                    expires_at, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (record.id, *values),
            )
            return
        connection.execute(
            """
            UPDATE api_keys SET
                tenant_id = ?, project_id = ?, name = ?, key_hash = ?, key_prefix = ?,
                scopes_json = ?, model_allowlist_json = ?, budget_limit_usd = ?,
                expires_at = ?, status = ?, created_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (*values, record.id),
        )

    def _insert_audit(
        self,
        connection: sqlite3.Connection,
        *,
        actor_id: str,
        tenant_id: str,
        action: str,
        target_type: str,
        target_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        created_at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                id, actor_id, tenant_id, action, target_type, target_id,
                before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"audit_{uuid4().hex}",
                actor_id,
                tenant_id,
                action,
                target_type,
                target_id,
                json.dumps(before, sort_keys=True, separators=(",", ":")) if before else None,
                json.dumps(after, sort_keys=True, separators=(",", ":")) if after else None,
                _format_control_datetime(created_at),
            ),
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
                    output_tokens, cost_usd, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    format_usage_timestamp(event.created_at),
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

        return [_usage_event_from_row(row) for row in rows]

    def query(self, query: UsageQuery) -> UsagePage:
        clauses = ["tenant_id = ?", "id > ?"]
        parameters: list[str | int] = [query.tenant_id, query.after_sequence_id]
        if query.start_time is not None:
            clauses.append("created_at >= ?")
            parameters.append(format_usage_timestamp(query.start_time))
        if query.end_time is not None:
            clauses.append("created_at <= ?")
            parameters.append(format_usage_timestamp(query.end_time))

        statement = f"SELECT * FROM usage_events WHERE {' AND '.join(clauses)} ORDER BY id"
        if query.limit is not None:
            statement += " LIMIT ?"
            parameters.append(query.limit + 1)

        with self.database.connect() as connection:
            rows = connection.execute(statement, parameters).fetchall()

        has_more = query.limit is not None and len(rows) > query.limit
        if has_more:
            rows = rows[: query.limit]
        return UsagePage(
            events=[_usage_event_from_row(row) for row in rows],
            has_more=has_more,
        )


def _usage_event_from_row(row: sqlite3.Row) -> UsageEvent:
    created_at = parse_usage_timestamp(row["created_at"])
    if created_at is None:
        raise ValueError("Persisted usage events require created_at.")
    return UsageEvent(
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
        created_at=created_at,
        sequence_id=row["id"],
    )


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


def _api_key_from_row(row: sqlite3.Row) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        project_id=row["project_id"],
        name=row["name"],
        key_hash=row["key_hash"],
        key_prefix=row["key_prefix"],
        scopes=tuple(json.loads(row["scopes_json"])),
        model_allowlist=tuple(json.loads(row["model_allowlist_json"])),
        budget_limit_usd=(
            Decimal(row["budget_limit_usd"]) if row["budget_limit_usd"] is not None else None
        ),
        expires_at=_parse_control_datetime(row["expires_at"]),
        status=row["status"],
        created_at=_require_control_datetime(row["created_at"]),
        updated_at=_require_control_datetime(row["updated_at"]),
    )


def _tenant_policy_from_row(row: sqlite3.Row) -> TenantPolicy:
    return TenantPolicy(
        tenant_id=row["tenant_id"],
        model_allowlist=tuple(json.loads(row["model_allowlist_json"])),
        routing_objective=row["routing_objective"],
        updated_at=_require_control_datetime(row["updated_at"]),
    )


def _audit_event_from_row(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        id=row["id"],
        actor_id=row["actor_id"],
        tenant_id=row["tenant_id"],
        action=row["action"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        before=json.loads(row["before_json"]) if row["before_json"] is not None else None,
        after=json.loads(row["after_json"]) if row["after_json"] is not None else None,
        created_at=_require_control_datetime(row["created_at"]),
    )


def _format_control_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_control_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _require_control_datetime(value: str) -> datetime:
    parsed = _parse_control_datetime(value)
    if parsed is None:
        raise ValueError("Persisted control-plane timestamps may not be null.")
    return parsed
