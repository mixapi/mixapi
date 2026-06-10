from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url
from sqlalchemy.sql.sqltypes import Numeric, TIMESTAMP


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TABLES = {
    "api_keys",
    "tenant_policies",
    "audit_events",
    "usage_events",
    "route_decisions",
    "budget_spend",
    "provider_connections",
    "logical_models",
    "logical_model_aliases",
    "model_candidates",
    "configuration_versions",
    "configuration_outbox",
    "usage_write_intents",
    "budget_reconciliation_outbox",
    "sqlite_migration_runs",
    "sqlite_migration_batches",
}


def test_alembic_creates_and_downgrades_complete_postgres_schema(database_url: str) -> None:
    source_url = make_url(database_url)
    database_name = f"mixapi_schema_{uuid4().hex}"
    test_url = source_url.set(database=database_name)
    admin_url = source_url.set(database="postgres")

    with psycopg.connect(admin_url.render_as_string(hide_password=False), autocommit=True) as connection:
        connection.execute(f'CREATE DATABASE "{database_name}"')

    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", test_url.render_as_string(hide_password=False))
        command.upgrade(config, "head")

        engine = create_engine(test_url.set(drivername="postgresql+psycopg"))
        schema = inspect(engine)
        assert EXPECTED_TABLES.issubset(set(schema.get_table_names()))

        provider_columns = {
            column["name"]: column for column in schema.get_columns("provider_connections")
        }
        usage_columns = {column["name"]: column for column in schema.get_columns("usage_events")}
        outbox_columns = {
            column["name"]: column for column in schema.get_columns("configuration_outbox")
        }

        assert isinstance(provider_columns["metadata"]["type"], JSONB)
        assert isinstance(provider_columns["created_at"]["type"], TIMESTAMP)
        assert provider_columns["created_at"]["type"].timezone is True
        assert isinstance(usage_columns["cost_usd"]["type"], Numeric)
        assert (usage_columns["cost_usd"]["type"].precision, usage_columns["cost_usd"]["type"].scale) == (
            20,
            8,
        )
        assert outbox_columns["id"]["autoincrement"] is True

        alias_unique_constraints = schema.get_unique_constraints("logical_model_aliases")
        assert any(constraint["column_names"] == ["alias"] for constraint in alias_unique_constraints)

        command.downgrade(config, "base")
        remaining = set(inspect(engine).get_table_names())
        assert not EXPECTED_TABLES.intersection(remaining)
        engine.dispose()
    finally:
        with psycopg.connect(
            admin_url.render_as_string(hide_password=False),
            autocommit=True,
        ) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (database_name,),
            )
            connection.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
