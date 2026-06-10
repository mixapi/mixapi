from __future__ import annotations

from copy import deepcopy
import json

import pytest
import redis
from fastapi.testclient import TestClient

from mixapi.app import create_app
from mixapi.bootstrap import (
    BootstrapService,
    BootstrapValidationError,
    load_bootstrap_document,
)
from mixapi.catalog import default_seed_document
from mixapi.postgres import PostgresPool
from mixapi.publication import ConfigurationPublisher
from mixapi.repositories.configuration import PostgresConfigurationRepository
from mixapi.runtime.snapshots import RedisSnapshotStore
from mixapi.secrets import CredentialCipher
from mixapi.settings import Settings


MASTER_KEY = bytes.fromhex("55" * 32)


@pytest.fixture
def bootstrap_runtime(database_url: str, redis_url: str):
    settings = Settings(
        database_url=database_url,
        redis_url=redis_url,
        master_key=MASTER_KEY,
        master_key_version=3,
        previous_master_keys={},
        admin_api_key="admin-secret",
        redis_namespace="bootstrap-test",
        configuration_publisher_interval_seconds=60,
        configuration_rebuild_interval_seconds=60,
    )
    pool = PostgresPool(settings)
    pool.open()
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    client.flushdb()
    with pool.connection() as connection:
        connection.execute(
            """
            TRUNCATE configuration_outbox, configuration_versions,
                     model_candidates, logical_model_aliases, logical_models,
                     provider_connections, audit_events
            RESTART IDENTITY CASCADE
            """
        )
    repository = PostgresConfigurationRepository(
        pool,
        CredentialCipher(MASTER_KEY, active_version=3),
    )
    snapshots = RedisSnapshotStore(client, namespace=settings.redis_namespace)
    publisher = ConfigurationPublisher(
        repository,
        snapshots,
        retention_count=3,
        retention_ttl_seconds=60,
    )
    bootstrap = BootstrapService(repository, publisher, snapshots)
    try:
        yield settings, bootstrap, snapshots, pool, client
    finally:
        client.flushdb()
        client.close()
        pool.close()


def test_empty_database_is_live_but_not_ready_and_public_routes_fail_closed(
    bootstrap_runtime,
) -> None:
    settings, _bootstrap, _snapshots, _pool, _client = bootstrap_runtime
    app = create_app(settings=settings)

    with TestClient(app) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        models = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer dev-key"},
        )

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 503
    assert ready.json()["status"] == "not_ready"
    assert ready.json()["checks"]["snapshot"] == "missing"
    assert models.status_code == 503
    assert models.json()["error"]["code"] == "configuration_unavailable"


def test_seed_is_idempotent_and_publishes_initial_snapshot(bootstrap_runtime) -> None:
    _settings, bootstrap, snapshots, pool, _client = bootstrap_runtime
    seed = default_seed_document(credential="bootstrap-secret")

    first = bootstrap.apply(seed, actor_id="bootstrap-test")
    with pool.connection() as connection:
        first_counts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM provider_connections) AS providers,
              (SELECT count(*) FROM logical_models) AS models,
              (SELECT count(*) FROM model_candidates) AS candidates,
              (SELECT count(*) FROM configuration_versions) AS versions
            """
        ).fetchone()
    second = bootstrap.apply(seed, actor_id="bootstrap-test")
    with pool.connection() as connection:
        second_counts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM provider_connections) AS providers,
              (SELECT count(*) FROM logical_models) AS models,
              (SELECT count(*) FROM model_candidates) AS candidates,
              (SELECT count(*) FROM configuration_versions) AS versions
            """
        ).fetchone()

    assert first.changed is True
    assert first.active_version == snapshots.active_version()
    assert snapshots.load(first.active_version).logical_models
    assert second.changed is False
    assert second.active_version == first.active_version
    assert second_counts == first_counts


def test_changed_seed_updates_only_changed_resource_and_publishes_new_version(
    bootstrap_runtime,
) -> None:
    _settings, bootstrap, snapshots, pool, _client = bootstrap_runtime
    seed = default_seed_document(credential="bootstrap-secret")
    first = bootstrap.apply(seed)
    changed = deepcopy(seed)
    changed["logical_models"][0]["description"] = "Updated description"

    second = bootstrap.apply(changed)

    assert second.changed is True
    assert second.active_version > first.active_version
    assert snapshots.load(second.active_version).logical_models[0].description == (
        "Updated description"
    )
    with pool.connection() as connection:
        versions = connection.execute(
            "SELECT count(*) AS count FROM configuration_versions"
        ).fetchone()
    assert versions["count"] == len(first.published_versions) + 1


def test_invalid_seed_is_rejected_before_any_rows_are_written(bootstrap_runtime) -> None:
    _settings, bootstrap, _snapshots, pool, _client = bootstrap_runtime
    invalid = default_seed_document(credential="bootstrap-secret")
    invalid["candidates"][0]["provider_connection_id"] = "missing-provider"

    with pytest.raises(BootstrapValidationError, match="provider"):
        bootstrap.apply(invalid)

    with pool.connection() as connection:
        counts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM provider_connections) AS providers,
              (SELECT count(*) FROM logical_models) AS models,
              (SELECT count(*) FROM model_candidates) AS candidates,
              (SELECT count(*) FROM configuration_versions) AS versions
            """
        ).fetchone()
    assert counts == {"providers": 0, "models": 0, "candidates": 0, "versions": 0}


def test_published_seed_makes_health_ready_and_drives_model_listing(
    bootstrap_runtime,
) -> None:
    settings, bootstrap, _snapshots, _pool, _client = bootstrap_runtime
    seed = default_seed_document(credential="bootstrap-secret")
    result = bootstrap.apply(seed)
    app = create_app(settings=settings)

    with TestClient(app) as client:
        ready = client.get("/health/ready")
        models = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer dev-key"},
        )

    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["active_version"] == result.active_version
    assert models.status_code == 200
    assert {model["id"] for model in models.json()["data"]} == {
        item["id"] for item in seed["logical_models"]
    }


def test_seed_documents_load_from_json_and_yaml(tmp_path) -> None:
    seed = default_seed_document(credential="bootstrap-secret")
    json_path = tmp_path / "seed.json"
    yaml_path = tmp_path / "seed.yaml"
    json_path.write_text(json.dumps(seed), encoding="utf-8")
    yaml_path.write_text(
        "schema_version: 1\nproviders: []\nlogical_models: []\ncandidates: []\n",
        encoding="utf-8",
    )

    assert load_bootstrap_document(json_path) == seed
    assert load_bootstrap_document(yaml_path)["schema_version"] == 1
