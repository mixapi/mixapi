from __future__ import annotations

import os

import psycopg
import pytest
import redis

from mixapi.bootstrap import BootstrapService
from mixapi.catalog import default_seed_document
from mixapi.postgres import PostgresPool
from mixapi.publication import ConfigurationPublisher
from mixapi.repositories.configuration import PostgresConfigurationRepository
from mixapi.runtime.snapshots import RedisSnapshotStore
from mixapi.secrets import CredentialCipher
from mixapi.settings import Settings


os.environ.setdefault(
    "MIXAPI_DATABASE_URL",
    "postgresql://mixapi:mixapi@127.0.0.1:55432/mixapi_test",
)
os.environ.setdefault("MIXAPI_REDIS_URL", "redis://127.0.0.1:56379/0")
os.environ.setdefault(
    "MIXAPI_MASTER_KEY",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
)
os.environ.setdefault("MIXAPI_MASTER_KEY_VERSION", "1")


@pytest.fixture(scope="session")
def database_url() -> str:
    value = os.getenv("MIXAPI_DATABASE_URL")
    assert value, "MIXAPI_DATABASE_URL is required for the test suite"
    return value


@pytest.fixture(scope="session")
def redis_url() -> str:
    value = os.getenv("MIXAPI_REDIS_URL")
    assert value, "MIXAPI_REDIS_URL is required for the test suite"
    return value


@pytest.fixture(autouse=True)
def reset_runtime_state(database_url: str, redis_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            TRUNCATE usage_events, route_decisions, budget_spend,
                     usage_write_intents, budget_reconciliation_outbox,
                     configuration_outbox, configuration_versions,
                     model_candidates, logical_model_aliases, logical_models,
                     provider_connections, audit_events
            RESTART IDENTITY CASCADE
            """
        )
    client = redis.Redis.from_url(redis_url)
    client.flushdb()
    client.close()

    settings = Settings.from_env()
    pool = PostgresPool(settings)
    pool.open()
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        cipher = CredentialCipher(
            settings.master_key,
            active_version=settings.master_key_version,
            previous_keys=settings.previous_master_keys,
        )
        repository = PostgresConfigurationRepository(pool, cipher)
        snapshots = RedisSnapshotStore(client, namespace=settings.redis_namespace)
        publisher = ConfigurationPublisher(
            repository,
            snapshots,
            retention_count=settings.snapshot_retention_count,
            retention_ttl_seconds=settings.snapshot_retention_ttl_seconds,
        )
        BootstrapService(repository, publisher, snapshots).apply(
            default_seed_document(credential="test-provider-secret"),
            actor_id="test-bootstrap",
        )
    finally:
        client.close()
        pool.close()
