from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import redis
from psycopg import Connection
from psycopg.types.json import Jsonb

from mixapi.bootstrap import BootstrapDocument
from mixapi.catalog import default_seed_document
from mixapi.configuration import ProviderConnection
from mixapi.postgres import PostgresPool
from mixapi.publication import ConfigurationPublisher, PublicationError
from mixapi.repositories.configuration import PostgresConfigurationRepository
from mixapi.runtime.snapshots import RedisSnapshotStore
from mixapi.secrets import CredentialAAD, CredentialCipher, EncryptedCredential
from mixapi.settings import Settings


class MigrationValidationError(ValueError):
    pass


class MigrationConflict(ValueError):
    pass


BatchHook = Callable[[str, str], None]
_PROVIDERS = {
    "openai": ("MIXAPI_OPENAI_BASE_URL", "MIXAPI_OPENAI_API_KEY"),
    "anthropic": ("MIXAPI_ANTHROPIC_BASE_URL", "MIXAPI_ANTHROPIC_API_KEY"),
    "gemini": ("MIXAPI_GEMINI_BASE_URL", "MIXAPI_GEMINI_API_KEY"),
    "ollama": ("MIXAPI_OLLAMA_BASE_URL", "MIXAPI_OLLAMA_API_KEY"),
}
_SQL_ENTITIES = (
    "api_keys",
    "tenant_policies",
    "audit_events",
    "usage_events",
    "route_decisions",
    "budget_spend",
)
_REDIS_ENTITIES = ("idempotency_records", "circuit_states")


class SQLiteMigrator:
    def __init__(
        self,
        settings: Settings,
        sqlite_path: str | Path,
        *,
        batch_size: int = 500,
        allow_disabled_providers: bool = False,
        batch_hook: BatchHook | None = None,
        verify_completed: bool = False,
    ) -> None:
        if batch_size <= 0:
            raise MigrationValidationError("batch size must be positive")
        self._settings = settings
        self._path = Path(sqlite_path).expanduser().resolve()
        self._batch_size = batch_size
        self._allow_disabled_providers = allow_disabled_providers
        self._batch_hook = batch_hook
        self._verify_completed = verify_completed
        self._cipher = CredentialCipher(
            settings.master_key,
            active_version=settings.master_key_version,
            previous_keys=settings.previous_master_keys,
        )

    def run(self, *, apply: bool = False) -> dict[str, Any]:
        fingerprint = _fingerprint(self._path)
        with closing(_open_read_only(self._path)) as source:
            source_summary = _source_summary(source)
            configuration = _configuration_plan()
            report = _base_report(fingerprint, source_summary, configuration, apply=apply)
            if not apply:
                return report
            self._validate_apply(source_summary, configuration)

            pool = PostgresPool(self._settings)
            pool.open()
            client = redis.Redis.from_url(
                self._settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=self._settings.dependency_connect_timeout_seconds,
                socket_timeout=self._settings.redis_socket_timeout_seconds,
            )
            try:
                if client.ping() is not True:
                    raise MigrationValidationError("Redis is unavailable")
                return self._apply(
                    source,
                    source_summary,
                    configuration,
                    fingerprint,
                    report,
                    pool,
                    client,
                )
            finally:
                client.close()
                pool.close()

    def _validate_apply(
        self,
        source_summary: dict[str, Any],
        configuration: dict[str, Any],
    ) -> None:
        if source_summary["counts"]["budget_reservations"]:
            raise MigrationValidationError(
                "legacy database has pending budget reservations; drain them before migration"
            )
        if configuration["disabled_providers"] and not self._allow_disabled_providers:
            raise MigrationValidationError(
                "real provider endpoints are missing; configure all legacy provider endpoints "
                "or pass --allow-disabled-providers"
            )

    def _apply(
        self,
        source: sqlite3.Connection,
        source_summary: dict[str, Any],
        configuration: dict[str, Any],
        fingerprint: str,
        report: dict[str, Any],
        pool: PostgresPool,
        client: redis.Redis,
    ) -> dict[str, Any]:
        run_id = f"sqlite_{fingerprint[:32]}"
        lock_key = int.from_bytes(bytes.fromhex(fingerprint[:16]), "big", signed=True)
        with pool.connection() as lock_connection:
            locked = lock_connection.execute(
                "SELECT pg_try_advisory_lock(%s)", (lock_key,)
            ).fetchone()["pg_try_advisory_lock"]
            if not locked:
                raise MigrationValidationError("this SQLite source is already being migrated")
            try:
                existing = self._start_run(pool, run_id, fingerprint)
                if existing["status"] == "completed":
                    stored = dict(existing["report"])
                    if self._verify_completed:
                        self._verify_completed_run(source, stored, pool, client)
                    return stored
                try:
                    version = self._migrate_configuration(
                        pool,
                        run_id,
                        configuration["document"],
                    )
                    provider_map = _provider_map(configuration["document"])
                    for entity in _SQL_ENTITIES:
                        self._migrate_sql_entity(
                            source,
                            pool,
                            run_id,
                            entity,
                            version,
                            provider_map,
                        )
                    for entity in _REDIS_ENTITIES:
                        self._migrate_redis_entity(
                            source,
                            pool,
                            client,
                            run_id,
                            entity,
                        )
                    self._reset_sequences(pool)
                    publication = self._publish(pool, client, version)
                    destination = _destination_summary(
                        source,
                        pool,
                        client,
                        self._settings.redis_namespace,
                    )
                    matched = _reconciles(source_summary, destination)
                    if not matched:
                        raise MigrationConflict("source and destination totals do not reconcile")
                    if _fingerprint(self._path) != fingerprint:
                        raise MigrationConflict("SQLite source changed during migration")
                    report.update(
                        {
                            "status": "completed",
                            "destination": destination,
                            "reconciliation": {"matched": True},
                        }
                    )
                    report["configuration"].update(
                        {
                            "version": version,
                            "active_version": publication["active_version"],
                            "publication_status": publication["status"],
                        }
                    )
                    self._complete_run(pool, run_id, report)
                    return report
                except Exception as error:
                    self._fail_run(pool, run_id, report, error)
                    raise
            finally:
                lock_connection.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))

    def _start_run(
        self,
        pool: PostgresPool,
        run_id: str,
        fingerprint: str,
    ) -> dict[str, Any]:
        with pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO sqlite_migration_runs (
                    id, source_fingerprint, source_path, status
                ) VALUES (%s, %s, %s, 'running')
                ON CONFLICT (source_fingerprint) DO UPDATE SET
                    status = CASE
                        WHEN sqlite_migration_runs.status = 'completed'
                        THEN 'completed' ELSE 'running' END,
                    updated_at = now()
                RETURNING *
                """,
                (run_id, fingerprint, str(self._path)),
            ).fetchone()
        return row

    def _migrate_configuration(
        self,
        pool: PostgresPool,
        run_id: str,
        document: dict[str, Any],
    ) -> int:
        completed = self._completed_batches(pool, run_id, "configuration")
        if completed:
            cursor = completed[-1]["source_cursor"]
            if not cursor or not cursor.startswith("version:"):
                raise MigrationConflict("configuration checkpoint is invalid")
            return int(cursor.removeprefix("version:"))

        with pool.connection() as connection:
            for provider in document["providers"]:
                self._insert_provider(connection, provider)
            for model in document["logical_models"]:
                self._insert_logical_model(connection, model)
            for candidate in document["candidates"]:
                self._insert_candidate(connection, candidate)
            version = connection.execute(
                "INSERT INTO configuration_versions DEFAULT VALUES RETURNING version"
            ).fetchone()["version"]
            connection.execute(
                """
                INSERT INTO configuration_outbox (
                    configuration_version, event_type, payload
                ) VALUES (%s, 'configuration.changed', %s)
                """,
                (version, Jsonb({"source": "sqlite-migration"})),
            )
            self._record_batch(
                connection,
                run_id,
                "configuration",
                f"version:{version}",
                len(document["providers"])
                + len(document["logical_models"])
                + len(document["candidates"]),
            )
        self._after_batch("configuration", f"version:{version}")
        return version

    def _insert_provider(self, connection: Connection, provider: dict[str, Any]) -> None:
        envelope = self._cipher.encrypt(
            provider["credential"],
            CredentialAAD(
                provider_id=provider["id"],
                protocol=provider["protocol"],
                field="api_key",
            ),
        )
        row = connection.execute(
            """
            INSERT INTO provider_connections (
                id, name, protocol, base_url, credential_ciphertext,
                credential_nonce, credential_key_version,
                credential_fingerprint, timeout_seconds, status,
                priority, weight, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING RETURNING *
            """,
            (
                provider["id"],
                provider["name"],
                provider["protocol"],
                provider["base_url"],
                envelope.ciphertext,
                envelope.nonce,
                envelope.key_version,
                envelope.fingerprint,
                provider["timeout_seconds"],
                provider["status"],
                provider["priority"],
                provider["weight"],
                Jsonb(provider["metadata"]),
            ),
        ).fetchone()
        if row is not None:
            return
        row = connection.execute(
            "SELECT * FROM provider_connections WHERE id = %s OR name = %s",
            (provider["id"], provider["name"]),
        ).fetchone()
        if row is None:
            raise MigrationConflict(f"provider_connections conflict: {provider['id']}")
        stored = ProviderConnection(
            id=row["id"],
            name=row["name"],
            protocol=row["protocol"],
            base_url=row["base_url"],
            credential=EncryptedCredential(
                ciphertext=bytes(row["credential_ciphertext"]),
                nonce=bytes(row["credential_nonce"]),
                key_version=row["credential_key_version"],
                fingerprint=row["credential_fingerprint"],
            ),
            timeout_seconds=row["timeout_seconds"],
            status=row["status"],
            priority=row["priority"],
            weight=row["weight"],
            metadata=row["metadata"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"],
        )
        plaintext = self._cipher.decrypt(
            stored.credential,
            CredentialAAD(
                provider_id=stored.id,
                protocol=stored.protocol,
                field="api_key",
            ),
        )
        expected = {
            "id": provider["id"],
            "name": provider["name"],
            "protocol": provider["protocol"],
            "base_url": provider["base_url"],
            "timeout_seconds": Decimal(provider["timeout_seconds"]),
            "status": provider["status"],
            "priority": provider["priority"],
            "weight": provider["weight"],
            "metadata": provider["metadata"],
            "credential": provider["credential"],
        }
        actual = {
            "id": stored.id,
            "name": stored.name,
            "protocol": stored.protocol,
            "base_url": stored.base_url,
            "timeout_seconds": stored.timeout_seconds,
            "status": stored.status,
            "priority": stored.priority,
            "weight": stored.weight,
            "metadata": stored.metadata,
            "credential": plaintext,
        }
        _require_equal("provider_connections", provider["id"], actual, expected)

    def _insert_logical_model(self, connection: Connection, model: dict[str, Any]) -> None:
        row = connection.execute(
            """
            INSERT INTO logical_models (id, description, status)
            VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING *
            """,
            (model["id"], model["description"], model["status"]),
        ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT * FROM logical_models WHERE id = %s", (model["id"],)
            ).fetchone()
            _require_equal(
                "logical_models",
                model["id"],
                _pick(row, ("id", "description", "status")),
                _pick(model, ("id", "description", "status")),
            )
        for alias in model.get("aliases", []):
            existing = connection.execute(
                """
                INSERT INTO logical_model_aliases (alias, logical_model_id)
                VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING alias
                """,
                (alias, model["id"]),
            ).fetchone()
            if existing is None:
                stored = connection.execute(
                    "SELECT logical_model_id FROM logical_model_aliases WHERE alias = %s",
                    (alias,),
                ).fetchone()
                _require_equal(
                    "logical_model_aliases",
                    alias,
                    stored["logical_model_id"] if stored else None,
                    model["id"],
                )

    def _insert_candidate(self, connection: Connection, candidate: dict[str, Any]) -> None:
        keys = (
            "id",
            "logical_model_id",
            "provider_connection_id",
            "upstream_model_id",
            "status",
            "priority",
            "weight",
            "context_window_tokens",
            "max_output_tokens",
            "input_modalities",
            "output_modalities",
            "tool_modes",
            "schema_support",
            "streaming_support",
            "embeddings_support",
            "retention_class",
            "regions",
            "pricing",
            "native_features",
            "unsupported_parameters",
        )
        values = [candidate[key] for key in keys]
        for index in (9, 10, 11, 16, 17, 18, 19):
            values[index] = Jsonb(values[index])
        row = connection.execute(
            f"""
            INSERT INTO model_candidates ({', '.join(keys)})
            VALUES ({', '.join(['%s'] * len(keys))})
            ON CONFLICT DO NOTHING RETURNING *
            """,
            values,
        ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT * FROM model_candidates WHERE id = %s",
                (candidate["id"],),
            ).fetchone()
            actual = _pick(row, keys)
            expected = _pick(candidate, keys)
            for key in (
                "input_modalities",
                "output_modalities",
                "tool_modes",
                "regions",
                "native_features",
                "unsupported_parameters",
            ):
                actual[key] = list(actual[key])
                expected[key] = list(expected[key])
            _require_equal("model_candidates", candidate["id"], actual, expected)

    def _migrate_sql_entity(
        self,
        source: sqlite3.Connection,
        pool: PostgresPool,
        run_id: str,
        entity: str,
        version: int,
        provider_map: dict[str, tuple[str, str]],
    ) -> None:
        rows = [dict(row) for row in source.execute(f"SELECT * FROM {entity} {_order(entity)}")]
        completed_count = sum(
            row["rows_imported"] for row in self._completed_batches(pool, run_id, entity)
        )
        if completed_count > len(rows):
            raise MigrationConflict(f"{entity} checkpoint exceeds source row count")
        for batch in _batches(rows[completed_count:], self._batch_size):
            cursor = _batch_cursor(completed_count, batch, entity)
            with pool.connection() as connection:
                for row in batch:
                    self._insert_legacy_row(
                        connection,
                        entity,
                        row,
                        version,
                        provider_map,
                    )
                self._record_batch(connection, run_id, entity, cursor, len(batch))
            self._after_batch(entity, cursor)
            completed_count += len(batch)

    def _insert_legacy_row(
        self,
        connection: Connection,
        entity: str,
        source: dict[str, Any],
        version: int,
        provider_map: dict[str, tuple[str, str]],
    ) -> None:
        if entity == "api_keys":
            expected = {
                "id": source["id"],
                "tenant_id": source["tenant_id"],
                "project_id": source["project_id"],
                "name": source["name"],
                "key_hash": source["key_hash"],
                "key_prefix": source["key_prefix"],
                "scopes": _json(source["scopes_json"]),
                "model_allowlist": _json(source["model_allowlist_json"]),
                "budget_limit_usd": _decimal(source["budget_limit_usd"]),
                "expires_at": _timestamp(source["expires_at"]),
                "status": source["status"],
                "created_at": _timestamp(source["created_at"]),
                "updated_at": _timestamp(source["updated_at"]),
            }
            self._insert_mapping(connection, entity, expected, ("id",))
            return
        if entity == "tenant_policies":
            expected = {
                "tenant_id": source["tenant_id"],
                "model_allowlist": _json(source["model_allowlist_json"]),
                "routing_objective": source["routing_objective"],
                "updated_at": _timestamp(source["updated_at"]),
            }
            self._insert_mapping(connection, entity, expected, ("tenant_id",))
            return
        if entity == "audit_events":
            expected = {
                "sequence_id": source["sequence_id"],
                "id": source["id"],
                "actor_id": source["actor_id"],
                "tenant_id": source["tenant_id"],
                "action": source["action"],
                "target_type": source["target_type"],
                "target_id": source["target_id"],
                "before": _json(source["before_json"]),
                "after": _json(source["after_json"]),
                "created_at": _timestamp(source["created_at"]),
            }
            self._insert_mapping(connection, entity, expected, ("sequence_id", "id"))
            return
        if entity == "usage_events":
            provider_id, protocol = _provider(provider_map, source["provider"])
            expected = {
                "id": source["id"],
                "request_id": source["request_id"],
                "tenant_id": source["tenant_id"],
                "project_id": source["project_id"],
                "api_key_id": source["api_key_id"],
                "endpoint": source["endpoint"],
                "logical_model": source["logical_model"],
                "provider_connection_id": provider_id,
                "provider_name": source["provider"],
                "provider_protocol": protocol,
                "provider_model": source["provider_model"],
                "configuration_version": version,
                "input_tokens": source["input_tokens"],
                "output_tokens": source["output_tokens"],
                "cost_usd": Decimal(source["cost_usd"]),
                "created_at": _timestamp(source["created_at"]),
            }
            self._insert_mapping(connection, entity, expected, ("id",))
            return
        if entity == "route_decisions":
            selected = source["selected_provider"]
            provider_id, protocol = (
                _provider(provider_map, selected) if selected else (None, None)
            )
            expected = {
                "tenant_id": source["tenant_id"],
                "request_id": source["request_id"],
                "project_id": source["project_id"],
                "endpoint": source["endpoint"],
                "logical_model": source["logical_model"],
                "configuration_version": version,
                "status": source["status"],
                "selected_provider_connection_id": provider_id,
                "selected_provider_name": selected,
                "selected_provider_protocol": protocol,
                "selected_provider_model": source["selected_provider_model"],
                "attempts": _json(source["attempts_json"]),
                "rejected_candidates": _json(source["rejected_candidates_json"]),
            }
            self._insert_mapping(
                connection,
                entity,
                expected,
                ("tenant_id", "request_id"),
            )
            return
        if entity == "budget_spend":
            expected = {
                "api_key_id": source["api_key_id"],
                "actual_spend_usd": Decimal(source["actual_spend_usd"]),
            }
            self._insert_mapping(connection, entity, expected, ("api_key_id",))
            return
        raise AssertionError(f"unsupported SQL migration entity: {entity}")

    def _insert_mapping(
        self,
        connection: Connection,
        entity: str,
        expected: dict[str, Any],
        identity_keys: tuple[str, ...],
    ) -> None:
        columns = tuple(expected)
        values = [
            Jsonb(value) if isinstance(value, (dict, list)) else value
            for value in expected.values()
        ]
        row = connection.execute(
            f"""
            INSERT INTO {entity} ({', '.join(columns)})
            VALUES ({', '.join(['%s'] * len(columns))})
            ON CONFLICT DO NOTHING RETURNING {', '.join(columns)}
            """,
            values,
        ).fetchone()
        identity = ":".join(str(expected[key]) for key in identity_keys)
        if row is not None:
            return
        clauses = " OR ".join(f"{key} = %s" for key in identity_keys)
        stored = connection.execute(
            f"SELECT {', '.join(columns)} FROM {entity} WHERE {clauses} LIMIT 1",
            tuple(expected[key] for key in identity_keys),
        ).fetchone()
        _require_equal(entity, identity, _pick(stored, columns), expected)

    def _migrate_redis_entity(
        self,
        source: sqlite3.Connection,
        pool: PostgresPool,
        client: redis.Redis,
        run_id: str,
        entity: str,
    ) -> None:
        rows = [dict(row) for row in source.execute(f"SELECT * FROM {entity} {_order(entity)}")]
        completed_count = sum(
            row["rows_imported"] for row in self._completed_batches(pool, run_id, entity)
        )
        if completed_count > len(rows):
            raise MigrationConflict(f"{entity} checkpoint exceeds source row count")
        for batch in _batches(rows[completed_count:], self._batch_size):
            for row in batch:
                if entity == "idempotency_records":
                    self._insert_idempotency(client, row)
                else:
                    self._insert_circuit(client, row)
            cursor = _batch_cursor(completed_count, batch, entity)
            with pool.connection() as connection:
                self._record_batch(connection, run_id, entity, cursor, len(batch))
            self._after_batch(entity, cursor)
            completed_count += len(batch)

    def _insert_idempotency(self, client: redis.Redis, row: dict[str, Any]) -> None:
        key = _idempotency_key(
            self._settings.redis_namespace,
            row["tenant_id"],
            row["endpoint"],
            row["idempotency_key"],
        )
        payload = json.dumps(
            {
                "version": 1,
                "request_hash": row["request_hash"],
                "response": _json(row["response_json"]),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        created = client.set(
            key,
            payload,
            nx=True,
            ex=self._settings.idempotency_ttl_seconds,
        )
        if not created and client.get(key) != payload:
            raise MigrationConflict("idempotency_records conflict")

    def _insert_circuit(self, client: redis.Redis, row: dict[str, Any]) -> None:
        key = _circuit_key(
            self._settings.redis_namespace,
            row["provider"],
            row["provider_model"],
        )
        expected = {"failures": str(row["consecutive_failures"])}
        if row["opened_at"] is not None:
            expected["opened_ms"] = str(round(float(row["opened_at"]) * 1000))
        stored = client.hgetall(key)
        if stored:
            _require_equal("circuit_states", _identity("circuit_states", row), stored, expected)
            return
        pipeline = client.pipeline()
        pipeline.hset(key, mapping=expected)
        pipeline.pexpire(key, 60_000)
        pipeline.execute()

    def _publish(
        self,
        pool: PostgresPool,
        client: redis.Redis,
        version: int,
    ) -> dict[str, Any]:
        repository = PostgresConfigurationRepository(pool, self._cipher)
        snapshots = RedisSnapshotStore(client, namespace=self._settings.redis_namespace)
        publisher = ConfigurationPublisher(
            repository,
            snapshots,
            retention_count=self._settings.snapshot_retention_count,
            retention_ttl_seconds=self._settings.snapshot_retention_ttl_seconds,
        )
        try:
            snapshot = publisher.publish(version)
        except PublicationError:
            if not self._allow_disabled_providers:
                raise
            return {
                "status": "disabled-providers-allowed",
                "active_version": snapshots.active_version(),
            }
        return {"status": "published", "active_version": snapshot.version}

    def _verify_completed_run(
        self,
        source: sqlite3.Connection,
        report: dict[str, Any],
        pool: PostgresPool,
        client: redis.Redis,
    ) -> None:
        version = report.get("configuration", {}).get("version")
        if not isinstance(version, int):
            raise MigrationConflict("completed migration report has no configuration version")
        provider_map = _provider_map(_configuration_plan()["document"])
        for entity in _SQL_ENTITIES:
            for row in source.execute(f"SELECT * FROM {entity} {_order(entity)}"):
                with pool.connection() as connection:
                    self._insert_legacy_row(
                        connection,
                        entity,
                        dict(row),
                        version,
                        provider_map,
                    )
        for row in source.execute(
            """
            SELECT * FROM idempotency_records
            ORDER BY tenant_id, endpoint, idempotency_key
            """
        ):
            self._insert_idempotency(client, dict(row))
        for row in source.execute("SELECT * FROM circuit_states ORDER BY provider, provider_model"):
            self._insert_circuit(client, dict(row))

    def _completed_batches(
        self,
        pool: PostgresPool,
        run_id: str,
        entity: str,
    ) -> list[dict[str, Any]]:
        with pool.connection() as connection:
            return connection.execute(
                """
                SELECT source_cursor, rows_imported
                FROM sqlite_migration_batches
                WHERE migration_run_id = %s AND entity = %s
                  AND status = 'completed'
                ORDER BY id
                """,
                (run_id, entity),
            ).fetchall()

    @staticmethod
    def _record_batch(
        connection: Connection,
        run_id: str,
        entity: str,
        cursor: str,
        rows_imported: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO sqlite_migration_batches (
                migration_run_id, entity, source_cursor,
                rows_imported, status, completed_at
            ) VALUES (%s, %s, %s, %s, 'completed', now())
            ON CONFLICT (migration_run_id, entity, source_cursor) DO NOTHING
            """,
            (run_id, entity, cursor, rows_imported),
        )

    @staticmethod
    def _reset_sequences(pool: PostgresPool) -> None:
        with pool.connection() as connection:
            for table, column in (("usage_events", "id"), ("audit_events", "sequence_id")):
                connection.execute(
                    f"""
                    SELECT setval(
                        pg_get_serial_sequence('{table}', '{column}'),
                        COALESCE((SELECT max({column}) FROM {table}), 1),
                        EXISTS(SELECT 1 FROM {table})
                    )
                    """
                )

    @staticmethod
    def _complete_run(pool: PostgresPool, run_id: str, report: dict[str, Any]) -> None:
        with pool.connection() as connection:
            connection.execute(
                """
                UPDATE sqlite_migration_runs
                SET status = 'completed', report = %s, updated_at = now(),
                    completed_at = now()
                WHERE id = %s
                """,
                (Jsonb(report), run_id),
            )

    @staticmethod
    def _fail_run(
        pool: PostgresPool,
        run_id: str,
        report: dict[str, Any],
        error: Exception,
    ) -> None:
        failed = dict(report)
        failed.update(
            {
                "status": "failed",
                "error": {
                    "type": type(error).__name__,
                    "message": str(error)[:500],
                },
            }
        )
        with pool.connection() as connection:
            connection.execute(
                """
                UPDATE sqlite_migration_runs
                SET status = 'failed', report = %s, updated_at = now()
                WHERE id = %s
                """,
                (Jsonb(failed), run_id),
            )

    def _after_batch(self, entity: str, cursor: str) -> None:
        if self._batch_hook is not None:
            self._batch_hook(entity, cursor)


def _configuration_plan() -> dict[str, Any]:
    document = default_seed_document(credential="")
    disabled: list[str] = []
    for provider in document["providers"]:
        endpoint_env, credential_env = _PROVIDERS[provider["name"]]
        endpoint = os.getenv(endpoint_env, "").strip()
        credential = os.getenv(credential_env, "")
        provider["credential"] = credential
        provider["metadata"] = {
            key: value
            for key, value in provider["metadata"].items()
            if key != "adapter"
        }
        provider["metadata"]["migrated_from"] = "sqlite"
        if endpoint:
            provider["base_url"] = endpoint
            provider["status"] = "active"
        else:
            provider["status"] = "disabled"
            disabled.append(provider["name"])
    statuses = {provider["id"]: provider["status"] for provider in document["providers"]}
    for candidate in document["candidates"]:
        candidate["status"] = statuses[candidate["provider_connection_id"]]
    active_by_model = {
        model["id"]: any(
            candidate["logical_model_id"] == model["id"]
            and candidate["status"] == "active"
            for candidate in document["candidates"]
        )
        for model in document["logical_models"]
    }
    for model in document["logical_models"]:
        model["status"] = "active" if active_by_model[model["id"]] else "disabled"
    BootstrapDocument.model_validate(document)
    return {
        "document": document,
        "disabled_providers": disabled,
        "publishable": not disabled,
    }


def _base_report(
    fingerprint: str,
    source_summary: dict[str, Any],
    configuration: dict[str, Any],
    *,
    apply: bool,
) -> dict[str, Any]:
    return {
        "mode": "apply" if apply else "dry-run",
        "status": "pending" if apply else "validated",
        "source": {"fingerprint": fingerprint, **source_summary},
        "configuration": {
            "publishable": configuration["publishable"],
            "disabled_providers": list(configuration["disabled_providers"]),
            "provider_count": len(configuration["document"]["providers"]),
            "logical_model_count": len(configuration["document"]["logical_models"]),
            "candidate_count": len(configuration["document"]["candidates"]),
        },
    }


def _source_summary(source: sqlite3.Connection) -> dict[str, Any]:
    tables = (
        "api_keys",
        "tenant_policies",
        "audit_events",
        "usage_events",
        "route_decisions",
        "budget_spend",
        "budget_reservations",
        "idempotency_records",
        "circuit_states",
    )
    counts = {
        table: source.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in tables
    }
    usage = source.execute(
        "SELECT input_tokens, output_tokens, cost_usd FROM usage_events ORDER BY id"
    ).fetchall()
    input_tokens = sum(row[0] for row in usage)
    output_tokens = sum(row[1] for row in usage)
    usage_cost = sum((Decimal(row[2]) for row in usage), Decimal("0"))
    spend = sum(
        (
            Decimal(row[0])
            for row in source.execute("SELECT actual_spend_usd FROM budget_spend")
        ),
        Decimal("0"),
    )
    return {
        "counts": counts,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": _money(usage_cost),
        },
        "settled_spend_usd": _money(spend),
    }


def _destination_summary(
    source: sqlite3.Connection,
    pool: PostgresPool,
    client: redis.Redis,
    namespace: str,
) -> dict[str, Any]:
    identities = {
        "api_keys": [row[0] for row in source.execute("SELECT id FROM api_keys")],
        "tenant_policies": [
            row[0] for row in source.execute("SELECT tenant_id FROM tenant_policies")
        ],
        "audit_events": [row[0] for row in source.execute("SELECT id FROM audit_events")],
        "usage_events": [row[0] for row in source.execute("SELECT id FROM usage_events")],
        "budget_spend": [row[0] for row in source.execute("SELECT api_key_id FROM budget_spend")],
    }
    counts: dict[str, int] = {}
    with pool.connection() as connection:
        for table, values in identities.items():
            column = {
                "api_keys": "id",
                "tenant_policies": "tenant_id",
                "audit_events": "id",
                "usage_events": "id",
                "budget_spend": "api_key_id",
            }[table]
            counts[table] = _count_values(connection, table, column, values)
        routes = [
            tuple(row)
            for row in source.execute(
                "SELECT tenant_id, request_id FROM route_decisions"
            )
        ]
        counts["route_decisions"] = sum(
            connection.execute(
                """
                SELECT count(*) AS count FROM route_decisions
                WHERE tenant_id = %s AND request_id = %s
                """,
                identity,
            ).fetchone()["count"]
            for identity in routes
        )
        usage_ids = identities["usage_events"]
        if usage_ids:
            totals = connection.execute(
                """
                SELECT COALESCE(sum(input_tokens), 0) AS input_tokens,
                       COALESCE(sum(output_tokens), 0) AS output_tokens,
                       COALESCE(sum(cost_usd), 0) AS cost_usd
                FROM usage_events WHERE id = ANY(%s)
                """,
                (usage_ids,),
            ).fetchone()
        else:
            totals = {"input_tokens": 0, "output_tokens": 0, "cost_usd": Decimal("0")}
        spend_ids = identities["budget_spend"]
        if spend_ids:
            spend = connection.execute(
                """
                SELECT COALESCE(sum(actual_spend_usd), 0) AS value
                FROM budget_spend WHERE api_key_id = ANY(%s)
                """,
                (spend_ids,),
            ).fetchone()["value"]
        else:
            spend = Decimal("0")
    idempotency_rows = source.execute(
        "SELECT tenant_id, endpoint, idempotency_key FROM idempotency_records"
    ).fetchall()
    counts["idempotency_records"] = sum(
        bool(client.exists(_idempotency_key(namespace, *row))) for row in idempotency_rows
    )
    circuit_rows = source.execute(
        "SELECT provider, provider_model FROM circuit_states"
    ).fetchall()
    counts["circuit_states"] = sum(
        bool(client.exists(_circuit_key(namespace, *row))) for row in circuit_rows
    )
    counts["budget_reservations"] = 0
    return {
        "counts": counts,
        "usage": {
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "cost_usd": _money(totals["cost_usd"]),
        },
        "settled_spend_usd": _money(spend),
    }


def _reconciles(source: dict[str, Any], destination: dict[str, Any]) -> bool:
    return (
        source["counts"] == destination["counts"]
        and source["usage"] == destination["usage"]
        and source["settled_spend_usd"] == destination["settled_spend_usd"]
    )


def _count_values(
    connection: Connection,
    table: str,
    column: str,
    values: list[Any],
) -> int:
    if not values:
        return 0
    return connection.execute(
        f"SELECT count(*) AS count FROM {table} WHERE {column} = ANY(%s)",
        (values,),
    ).fetchone()["count"]


def _fingerprint(path: Path) -> str:
    if not path.is_file():
        raise MigrationValidationError(f"SQLite source does not exist: {path}")
    digest = hashlib.sha256()
    sources = [path]
    wal_path = Path(f"{path}-wal")
    if wal_path.is_file():
        sources.append(wal_path)
    for source_path in sources:
        digest.update(source_path.name.encode("utf-8"))
        digest.update(b"\0")
        with source_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _json(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _money(value: Decimal | str | int) -> str:
    return format(Decimal(value).quantize(Decimal("0.00000001")), "f")


def _batches(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for index in range(0, len(rows), size):
        yield rows[index : index + size]


def _order(entity: str) -> str:
    return {
        "api_keys": "ORDER BY id",
        "tenant_policies": "ORDER BY tenant_id",
        "audit_events": "ORDER BY sequence_id",
        "usage_events": "ORDER BY id",
        "route_decisions": "ORDER BY tenant_id, request_id",
        "budget_spend": "ORDER BY api_key_id",
        "idempotency_records": "ORDER BY tenant_id, endpoint, idempotency_key",
        "circuit_states": "ORDER BY provider, provider_model",
    }[entity]


def _identity(entity: str, row: dict[str, Any]) -> str:
    keys = {
        "api_keys": ("id",),
        "tenant_policies": ("tenant_id",),
        "audit_events": ("sequence_id",),
        "usage_events": ("id",),
        "route_decisions": ("tenant_id", "request_id"),
        "budget_spend": ("api_key_id",),
        "idempotency_records": ("tenant_id", "endpoint", "idempotency_key"),
        "circuit_states": ("provider", "provider_model"),
    }[entity]
    return "\x1f".join(str(row[key]) for key in keys)


def _batch_cursor(
    completed_count: int,
    batch: list[dict[str, Any]],
    entity: str,
) -> str:
    identity = _identity(entity, batch[-1])
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{completed_count + len(batch)}:{digest}"


def _provider_map(document: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {
        provider["name"]: (provider["id"], provider["protocol"])
        for provider in document["providers"]
    }


def _provider(
    provider_map: dict[str, tuple[str, str]],
    name: str,
) -> tuple[str, str]:
    try:
        return provider_map[name]
    except KeyError as error:
        raise MigrationValidationError(f"unknown legacy provider: {name}") from error


def _pick(row: Any, keys: Iterable[str]) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: row[key] for key in keys}


def _require_equal(
    entity: str,
    identity: str,
    actual: Any,
    expected: Any,
) -> None:
    if actual != expected:
        raise MigrationConflict(f"{entity} destination row diverges: {identity}")


def _idempotency_key(namespace: str, tenant_id: str, endpoint: str, key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"{namespace}:v1:idempotency:{tenant_id}:{endpoint}:{digest}"


def _circuit_key(namespace: str, provider: str, provider_model: str) -> str:
    digest = hashlib.sha256(f"{provider}\0{provider_model}".encode()).hexdigest()
    return f"{namespace}:v1:circuit:{digest}"
