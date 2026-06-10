from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import redis

from mixapi.configuration import StoredConfiguration
from mixapi.postgres import PostgresPool
from mixapi.publication import ConfigurationPublisher, PublicationError
from mixapi.repositories.configuration import PostgresConfigurationRepository
from mixapi.runtime.snapshots import RedisSnapshotStore
from mixapi.secrets import CredentialCipher, EncryptedCredential
from mixapi.settings import Settings
from mixapi.workers import ConfigurationOutboxWorker, ConfigurationRebuildWorker


MASTER_KEY = bytes.fromhex("66" * 32)


@pytest.fixture
def publication_runtime(database_url: str, redis_url: str):
    settings = Settings(
        database_url=database_url,
        redis_url=redis_url,
        master_key=MASTER_KEY,
        master_key_version=4,
        previous_master_keys={},
        redis_namespace="publication-test",
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
        CredentialCipher(MASTER_KEY, active_version=4),
    )
    snapshots = RedisSnapshotStore(client, namespace=settings.redis_namespace)
    publisher = ConfigurationPublisher(
        repository,
        snapshots,
        retention_count=3,
        retention_ttl_seconds=60,
    )
    try:
        yield repository, snapshots, publisher, pool, client
    finally:
        client.flushdb()
        client.close()
        pool.close()


def _seed_complete(repository: PostgresConfigurationRepository):
    provider = repository.create_provider(
        name="gemini-primary",
        protocol="gemini",
        base_url="https://gemini.example/v1",
        credential="gemini-secret",
        timeout_seconds=Decimal("20"),
        actor_id="admin",
    ).resource
    model = repository.create_logical_model(
        model_id="gpt-5.5",
        description="Portable logical model",
        aliases=("reasoning-latest",),
        actor_id="admin",
    ).resource
    mutation = repository.create_candidate(
        logical_model_id=model.id,
        provider_connection_id=provider.id,
        upstream_model_id="gemini-2.5-pro",
        priority=10,
        weight=2,
        context_window_tokens=1_000_000,
        max_output_tokens=65_536,
        input_modalities=("text", "image"),
        output_modalities=("text",),
        tool_modes=("auto",),
        schema_support="strict_json_schema",
        streaming_support=True,
        embeddings_support=False,
        retention_class="standard",
        regions=("us",),
        pricing={"input_per_million": "1.25"},
        native_features=("thinking",),
        unsupported_parameters=(),
        actor_id="admin",
    )
    return provider, model, mutation


def test_workers_claim_disjoint_rows_and_expired_claims_are_recoverable(
    publication_runtime,
) -> None:
    repository, _snapshots, _publisher, pool, _client = publication_runtime
    repository.create_provider(
        name="vendor-a",
        protocol="gemini",
        base_url="https://a.example",
        credential="secret-a",
        actor_id="admin",
    )
    repository.create_provider(
        name="vendor-b",
        protocol="anthropic",
        base_url="https://b.example",
        credential="secret-b",
        actor_id="admin",
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                lambda worker: repository.claim_outbox(worker, limit=1, lease_seconds=60),
                ("worker-a", "worker-b"),
            )
        )
    claimed_ids = {events[0].id for events in claims}
    assert len(claimed_ids) == 2

    expired_id = claims[0][0].id
    with pool.connection() as connection:
        connection.execute(
            """
            UPDATE configuration_outbox
            SET claim_expires_at = now() - %s
            WHERE id = %s
            """,
            (timedelta(seconds=1), expired_id),
        )
    recovered = repository.claim_outbox("worker-c", limit=1, lease_seconds=60)
    assert [event.id for event in recovered] == [expired_id]


def test_complete_configuration_publishes_and_marks_version_active(
    publication_runtime,
) -> None:
    repository, snapshots, publisher, pool, _client = publication_runtime
    _provider, _model, mutation = _seed_complete(repository)

    snapshot = publisher.publish(mutation.version.version)

    assert snapshots.active_version() == mutation.version.version
    assert snapshots.load(mutation.version.version) == snapshot
    with pool.connection() as connection:
        version = connection.execute(
            """
            SELECT status, checksum, published_at
            FROM configuration_versions WHERE version = %s
            """,
            (mutation.version.version,),
        ).fetchone()
        outbox = connection.execute(
            """
            SELECT processed_at FROM configuration_outbox
            WHERE configuration_version = %s
            """,
            (mutation.version.version,),
        ).fetchone()
    assert version["status"] == "published"
    assert version["checksum"] == snapshot.checksum
    assert version["published_at"] is not None
    assert outbox["processed_at"] is not None


class _ConfigurationOverride:
    def __init__(self, repository, transform):
        self._repository = repository
        self._transform = transform

    def load_publication_configuration(self, version):
        record, configuration = self._repository.load_publication_configuration(version)
        return record, self._transform(configuration)

    def __getattr__(self, name):
        return getattr(self._repository, name)


def _replace_provider(configuration: StoredConfiguration, **changes) -> StoredConfiguration:
    return replace(
        configuration,
        providers=(replace(configuration.providers[0], **changes),),
    )


@pytest.mark.parametrize(
    ("transform", "error_code"),
    [
        (lambda value: replace(value, aliases={"gpt-5.5": "missing-model"}), "invalid_alias"),
        (lambda value: replace(value, candidates=()), "missing_model_candidate"),
        (
            lambda value: _replace_provider(value, base_url="http://private.example"),
            "invalid_provider_url",
        ),
        (
            lambda value: _replace_provider(value, protocol="unknown"),
            "unsupported_provider_protocol",
        ),
        (
            lambda value: _replace_provider(
                value,
                credential=EncryptedCredential(
                    key_version=999,
                    nonce=b"0123456789ab",
                    ciphertext=b"invalid",
                    fingerprint="invalid",
                ),
            ),
            "invalid_provider_credential",
        ),
    ],
)
def test_invalid_configuration_is_marked_failed_without_secret_details(
    publication_runtime,
    transform,
    error_code,
) -> None:
    repository, snapshots, _publisher, pool, _client = publication_runtime
    _provider, _model, mutation = _seed_complete(repository)
    publisher = ConfigurationPublisher(
        _ConfigurationOverride(repository, transform),
        snapshots,
        retention_count=3,
        retention_ttl_seconds=60,
    )

    with pytest.raises(PublicationError) as captured:
        publisher.publish(mutation.version.version)

    assert captured.value.code == error_code
    assert snapshots.active_version() is None
    with pool.connection() as connection:
        version = connection.execute(
            """
            SELECT status, error_code, error_message
            FROM configuration_versions WHERE version = %s
            """,
            (mutation.version.version,),
        ).fetchone()
    assert version["status"] == "failed"
    assert version["error_code"] == error_code
    assert "secret" not in version["error_message"].lower()
    assert "ciphertext" not in version["error_message"].lower()


def test_failed_publication_preserves_previous_active_snapshot(publication_runtime) -> None:
    repository, snapshots, publisher, _pool, _client = publication_runtime
    _provider, _model, valid = _seed_complete(repository)
    publisher.publish(valid.version.version)
    previous_checksum = snapshots.load(valid.version.version).checksum
    invalid = repository.create_logical_model(
        model_id="candidate-less",
        description="Invalid until mapped",
        aliases=(),
        actor_id="admin",
    )

    with pytest.raises(PublicationError) as captured:
        publisher.publish(invalid.version.version)

    assert captured.value.code == "missing_model_candidate"
    assert snapshots.active_version() == valid.version.version
    assert snapshots.load(valid.version.version).checksum == previous_checksum


def test_duplicate_events_publish_one_snapshot(publication_runtime) -> None:
    repository, snapshots, publisher, pool, client = publication_runtime
    _provider, _model, mutation = _seed_complete(repository)
    with pool.connection() as connection:
        connection.execute(
            """
            UPDATE configuration_outbox SET processed_at = now()
            WHERE configuration_version <> %s
            """,
            (mutation.version.version,),
        )
        connection.execute(
            """
            INSERT INTO configuration_outbox (
                configuration_version, event_type, payload
            ) VALUES (%s, 'configuration.changed', '{}')
            """,
            (mutation.version.version,),
        )
    worker = ConfigurationOutboxWorker(
        repository,
        publisher,
        worker_id="publisher-a",
        claim_limit=10,
        lease_seconds=60,
    )

    assert worker.run_once() == 1

    assert snapshots.active_version() == mutation.version.version
    assert len(client.keys("publication-test:v1:snapshots:*:payload")) == 1
    with pool.connection() as connection:
        pending = connection.execute(
            """
            SELECT count(*) AS count FROM configuration_outbox
            WHERE configuration_version = %s AND processed_at IS NULL
            """,
            (mutation.version.version,),
        ).fetchone()
    assert pending["count"] == 0


def test_periodic_rebuild_repairs_deleted_snapshot(publication_runtime) -> None:
    repository, snapshots, publisher, _pool, client = publication_runtime
    _provider, _model, mutation = _seed_complete(repository)
    published = publisher.publish(mutation.version.version)
    client.delete(
        f"publication-test:v1:snapshots:{published.version}:payload",
        f"publication-test:v1:snapshots:{published.version}:manifest",
    )
    rebuild = ConfigurationRebuildWorker(publisher)

    assert rebuild.run_once() is True

    assert snapshots.active_version() == published.version
    assert snapshots.load(published.version).checksum == published.checksum
