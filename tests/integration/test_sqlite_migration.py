from __future__ import annotations

import json
import shutil
from pathlib import Path

import psycopg
import pytest
import redis
from psycopg.rows import dict_row

from mixapi.cli import main
from mixapi.migration.sqlite_import import (
    MigrationConflict,
    MigrationValidationError,
    SQLiteMigrator,
)
from mixapi.settings import Settings


FIXTURE = Path(__file__).parents[1] / "fixtures" / "legacy_mixapi.sqlite3"
PROVIDER_ENV = {
    "MIXAPI_OPENAI_BASE_URL": "https://openai.example.net/v1",
    "MIXAPI_OPENAI_API_KEY": "openai-migration-secret",
    "MIXAPI_ANTHROPIC_BASE_URL": "https://anthropic.example.net",
    "MIXAPI_ANTHROPIC_API_KEY": "anthropic-migration-secret",
    "MIXAPI_GEMINI_BASE_URL": "https://gemini.example.net",
    "MIXAPI_GEMINI_API_KEY": "gemini-migration-secret",
    "MIXAPI_OLLAMA_BASE_URL": "https://ollama.example.net",
    "MIXAPI_OLLAMA_API_KEY": "ollama-migration-secret",
}


@pytest.fixture
def legacy_database(tmp_path: Path) -> Path:
    destination = tmp_path / "legacy.sqlite3"
    shutil.copyfile(FIXTURE, destination)
    return destination


@pytest.fixture
def migration_settings(database_url: str, redis_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        redis_url=redis_url,
        master_key=bytes.fromhex("44" * 32),
        master_key_version=9,
        redis_namespace="migration-test",
    )


@pytest.fixture
def empty_destination(database_url: str, redis_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            TRUNCATE sqlite_migration_batches, sqlite_migration_runs,
                     api_keys, tenant_policies, usage_events, route_decisions,
                     budget_spend, usage_write_intents,
                     budget_reconciliation_outbox, configuration_outbox,
                     configuration_versions, model_candidates,
                     logical_model_aliases, logical_models,
                     provider_connections, audit_events
            RESTART IDENTITY CASCADE
            """
        )
    client = redis.Redis.from_url(redis_url)
    client.flushdb()
    client.close()


def test_dry_run_writes_nothing_and_redacts_sensitive_values(
    legacy_database: Path,
    migration_settings: Settings,
    empty_destination: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_providers(monkeypatch)

    report = SQLiteMigrator(migration_settings, legacy_database, batch_size=1).run()

    assert report["mode"] == "dry-run"
    assert report["source"]["counts"]["usage_events"] == 2
    assert report["source"]["usage"]["input_tokens"] == 15
    assert report["source"]["usage"]["cost_usd"] == "0.00125000"
    assert report["configuration"]["publishable"] is True
    serialized = json.dumps(report, sort_keys=True)
    assert "migration-secret" not in serialized
    assert "a" * 64 not in serialized
    assert "idem-legacy" not in serialized

    with psycopg.connect(migration_settings.database_url) as connection:
        assert connection.execute("SELECT count(*) FROM sqlite_migration_runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM api_keys").fetchone()[0] == 0
    client = redis.Redis.from_url(migration_settings.redis_url)
    assert client.dbsize() == 0
    client.close()


def test_apply_preserves_rows_reconciles_totals_and_restores_redis(
    legacy_database: Path,
    migration_settings: Settings,
    empty_destination: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_providers(monkeypatch)

    report = SQLiteMigrator(migration_settings, legacy_database, batch_size=1).run(
        apply=True
    )

    assert report["status"] == "completed"
    assert report["reconciliation"]["matched"] is True
    assert report["configuration"]["active_version"] > 0
    assert report["destination"]["counts"]["usage_events"] == 2
    assert report["destination"]["usage"] == report["source"]["usage"]
    assert report["destination"]["settled_spend_usd"] == "0.00125000"

    with psycopg.connect(
        migration_settings.database_url,
        row_factory=dict_row,
    ) as connection:
        api_key = connection.execute(
            "SELECT * FROM api_keys WHERE id = 'key_legacy_001'"
        ).fetchone()
        assert api_key["created_at"].isoformat() == "2025-01-02T03:04:05.123456+00:00"
        assert api_key["updated_at"].isoformat() == "2025-01-02T03:05:06.654321+00:00"
        usage = connection.execute(
            "SELECT * FROM usage_events WHERE id = 11"
        ).fetchone()
        assert usage["created_at"].isoformat() == "2025-02-03T04:05:06.123456+00:00"
        assert usage["provider_connection_id"] == "provider_openai"
        assert usage["provider_protocol"] == "openai-compatible"
        assert usage["configuration_version"] == report["configuration"]["active_version"]
        audit = connection.execute(
            "SELECT * FROM audit_events WHERE id = 'audit_legacy_001'"
        ).fetchone()
        assert audit["sequence_id"] == 7
        assert audit["created_at"].isoformat() == "2025-01-04T05:06:07.111222+00:00"
        route = connection.execute(
            "SELECT * FROM route_decisions WHERE request_id = 'req-legacy-1'"
        ).fetchone()
        assert route["selected_provider_connection_id"] == "provider_openai"
        assert route["selected_provider_protocol"] == "openai-compatible"
        provider = connection.execute(
            "SELECT * FROM provider_connections WHERE id = 'provider_openai'"
        ).fetchone()
        assert provider["base_url"] == PROVIDER_ENV["MIXAPI_OPENAI_BASE_URL"]
        assert provider["status"] == "active"
        assert connection.execute(
            """
            SELECT count(*) AS count FROM model_candidates
            WHERE provider_connection_id = 'provider_openai' AND status = 'active'
            """
        ).fetchone()["count"] == 2

    client = redis.Redis.from_url(migration_settings.redis_url, decode_responses=True)
    idempotency_keys = list(
        client.scan_iter(match="migration-test:v1:idempotency:tenant_legacy:*")
    )
    assert len(idempotency_keys) == 1
    payload = json.loads(client.get(idempotency_keys[0]))
    assert payload == {
        "request_hash": "legacy-request-hash",
        "response": {"id": "resp_legacy", "status": "completed"},
        "version": 1,
    }
    circuit_keys = list(client.scan_iter(match="migration-test:v1:circuit:*"))
    assert len(circuit_keys) == 1
    assert client.hgetall(circuit_keys[0])["failures"] == "3"
    client.close()

    serialized = json.dumps(report, sort_keys=True)
    assert "migration-secret" not in serialized
    assert "a" * 64 not in serialized


def test_rerun_is_idempotent_and_changed_source_rows_are_rejected(
    legacy_database: Path,
    migration_settings: Settings,
    empty_destination: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_providers(monkeypatch)
    migrator = SQLiteMigrator(migration_settings, legacy_database, batch_size=1)

    first = migrator.run(apply=True)
    second = migrator.run(apply=True)

    assert second == first
    with psycopg.connect(migration_settings.database_url) as connection:
        assert connection.execute("SELECT count(*) FROM usage_events").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM sqlite_migration_runs").fetchone()[0] == 1

    with psycopg.connect(migration_settings.database_url) as connection:
        connection.execute(
            "UPDATE usage_events SET output_tokens = 999 WHERE id = 11"
        )
    with pytest.raises(MigrationConflict, match="usage_events"):
        SQLiteMigrator(
            migration_settings,
            legacy_database,
            batch_size=1,
            verify_completed=True,
        ).run(apply=True)


def test_interrupted_batches_resume_from_the_ledger(
    legacy_database: Path,
    migration_settings: Settings,
    empty_destination: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_providers(monkeypatch)
    interrupted = False

    def stop_after_first_usage_batch(entity: str, cursor: str) -> None:
        nonlocal interrupted
        if entity == "usage_events" and not interrupted:
            interrupted = True
            raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        SQLiteMigrator(
            migration_settings,
            legacy_database,
            batch_size=1,
            batch_hook=stop_after_first_usage_batch,
        ).run(apply=True)

    with psycopg.connect(migration_settings.database_url) as connection:
        assert connection.execute("SELECT count(*) FROM usage_events").fetchone()[0] == 1
        completed = connection.execute(
            """
            SELECT count(*) FROM sqlite_migration_batches
            WHERE entity = 'usage_events' AND status = 'completed'
            """
        ).fetchone()[0]
        assert completed == 1

    report = SQLiteMigrator(
        migration_settings,
        legacy_database,
        batch_size=1,
    ).run(apply=True)
    assert report["status"] == "completed"
    with psycopg.connect(migration_settings.database_url) as connection:
        assert connection.execute("SELECT count(*) FROM usage_events").fetchone()[0] == 2
        assert connection.execute(
            """
            SELECT count(*) FROM sqlite_migration_batches
            WHERE entity = 'usage_events' AND status = 'completed'
            """
        ).fetchone()[0] == 2


def test_missing_endpoints_block_apply_unless_disabled_providers_are_allowed(
    legacy_database: Path,
    migration_settings: Settings,
    empty_destination: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)

    dry_run = SQLiteMigrator(migration_settings, legacy_database).run()
    assert dry_run["configuration"]["publishable"] is False
    assert set(dry_run["configuration"]["disabled_providers"]) == {
        "openai",
        "anthropic",
        "gemini",
        "ollama",
    }
    with pytest.raises(MigrationValidationError, match="provider endpoints"):
        SQLiteMigrator(migration_settings, legacy_database).run(apply=True)
    with psycopg.connect(migration_settings.database_url) as connection:
        assert connection.execute("SELECT count(*) FROM sqlite_migration_runs").fetchone()[0] == 0

    report = SQLiteMigrator(
        migration_settings,
        legacy_database,
        allow_disabled_providers=True,
    ).run(apply=True)
    assert report["status"] == "completed"
    assert report["configuration"]["active_version"] is None
    assert report["configuration"]["publication_status"] == "disabled-providers-allowed"
    with psycopg.connect(migration_settings.database_url) as connection:
        statuses = dict(connection.execute("SELECT name, status FROM provider_connections"))
    assert statuses == {
        "anthropic": "disabled",
        "gemini": "disabled",
        "ollama": "disabled",
        "openai": "disabled",
    }


def test_cli_uses_postgres_dsn_argument_and_writes_redacted_report(
    legacy_database: Path,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_providers(monkeypatch)
    monkeypatch.delenv("MIXAPI_DATABASE_URL", raising=False)
    report_path = tmp_path / "reports" / "migration.json"

    result = main(
        [
            "migrate-sqlite",
            "--sqlite-path",
            str(legacy_database),
            "--postgres-dsn",
            database_url,
            "--batch-size",
            "1",
            "--report",
            str(report_path),
        ]
    )

    assert result == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["mode"] == "dry-run"
    assert json.loads(capsys.readouterr().out) == report
    encoded = report_path.read_text(encoding="utf-8")
    assert "migration-secret" not in encoded
    assert "a" * 64 not in encoded


def _configure_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in PROVIDER_ENV.items():
        monkeypatch.setenv(name, value)
