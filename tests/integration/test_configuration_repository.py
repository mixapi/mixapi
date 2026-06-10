from __future__ import annotations

from decimal import Decimal

import pytest

from mixapi.postgres import PostgresPool
from mixapi.repositories.configuration import (
    ConfigurationConflict,
    PostgresConfigurationRepository,
)
from mixapi.secrets import CredentialAAD, CredentialCipher
from mixapi.settings import Settings


MASTER_KEY = bytes.fromhex("44" * 32)


@pytest.fixture
def configuration_repository(database_url: str, redis_url: str):
    settings = Settings(
        database_url=database_url,
        redis_url=redis_url,
        master_key=MASTER_KEY,
        master_key_version=7,
        previous_master_keys={},
    )
    pool = PostgresPool(settings)
    pool.open()
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
        CredentialCipher(MASTER_KEY, active_version=7),
    )
    try:
        yield repository, pool
    finally:
        pool.close()


def test_provider_mutation_is_atomic_encrypted_and_redacted(configuration_repository) -> None:
    repository, pool = configuration_repository

    created = repository.create_provider(
        name="gemini-vendor-a",
        protocol="gemini",
        base_url="https://vendor.example",
        credential="provider-secret",
        timeout_seconds=Decimal("12.5"),
        priority=20,
        weight=3,
        metadata={"region": "us"},
        actor_id="admin",
    )

    assert created.resource.name == "gemini-vendor-a"
    assert created.resource.updated_at == created.version.created_at
    assert created.resource.public_dict()["credential_configured"] is True
    assert "provider-secret" not in str(created.resource.public_dict())

    with pool.connection() as connection:
        provider_row = connection.execute(
            "SELECT * FROM provider_connections WHERE id = %s",
            (created.resource.id,),
        ).fetchone()
        audit_row = connection.execute(
            "SELECT * FROM audit_events WHERE id = %s",
            (created.audit_id,),
        ).fetchone()
        outbox_row = connection.execute(
            "SELECT * FROM configuration_outbox WHERE id = %s",
            (created.outbox_id,),
        ).fetchone()

    assert provider_row["credential_ciphertext"] != b"provider-secret"
    assert audit_row["target_id"] == created.resource.id
    assert "provider-secret" not in str(audit_row["after"])
    assert outbox_row["configuration_version"] == created.version.version
    assert outbox_row["processed_at"] is None


def test_provider_credential_rotation_publishes_a_new_version(configuration_repository) -> None:
    repository, _pool = configuration_repository
    created = repository.create_provider(
        name="anthropic-vendor",
        protocol="anthropic",
        base_url="https://anthropic.example",
        credential="old-secret",
        actor_id="admin",
    )

    rotated = repository.rotate_provider_credential(
        created.resource.id,
        "new-secret",
        actor_id="admin",
    )

    assert rotated.version.version > created.version.version
    assert rotated.resource.credential.ciphertext != created.resource.credential.ciphertext
    assert repository.decrypt_provider_credential(rotated.resource) == "new-secret"
    assert rotated.resource.credential.fingerprint != created.resource.credential.fingerprint


def test_alias_conflict_rolls_back_model_version_audit_and_outbox(configuration_repository) -> None:
    repository, pool = configuration_repository
    repository.create_logical_model(
        model_id="model-a",
        description="First model",
        aliases=("shared-alias",),
        actor_id="admin",
    )
    with pool.connection() as connection:
        counts_before = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM logical_models),
              (SELECT count(*) FROM configuration_versions),
              (SELECT count(*) FROM audit_events),
              (SELECT count(*) FROM configuration_outbox)
            """
        ).fetchone()

    with pytest.raises(ConfigurationConflict, match="alias"):
        repository.create_logical_model(
            model_id="model-b",
            description="Second model",
            aliases=("shared-alias",),
            actor_id="admin",
        )

    with pool.connection() as connection:
        counts_after = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM logical_models),
              (SELECT count(*) FROM configuration_versions),
              (SELECT count(*) FROM audit_events),
              (SELECT count(*) FROM configuration_outbox)
            """
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM logical_models WHERE id = 'model-b'"
        ).fetchone() is None

    assert counts_after == counts_before


def test_candidates_load_as_complete_active_configuration(configuration_repository) -> None:
    repository, _pool = configuration_repository
    provider = repository.create_provider(
        name="openai-proxy",
        protocol="openai-compatible",
        base_url="https://proxy.example/v1",
        credential="proxy-secret",
        actor_id="admin",
    ).resource
    model = repository.create_logical_model(
        model_id="gpt-5.5",
        description="Portable alias",
        aliases=("premium-chat",),
        actor_id="admin",
    ).resource

    candidate = repository.create_candidate(
        logical_model_id=model.id,
        provider_connection_id=provider.id,
        upstream_model_id="vendor/gpt-compatible",
        priority=10,
        weight=4,
        context_window_tokens=128_000,
        max_output_tokens=16_384,
        input_modalities=("text", "image"),
        output_modalities=("text",),
        tool_modes=("function",),
        schema_support="strict_json_schema",
        streaming_support=True,
        embeddings_support=False,
        retention_class="standard",
        regions=("us",),
        pricing={"input_per_million": "1.00", "output_per_million": "2.00"},
        native_features=("responses",),
        unsupported_parameters=(),
        actor_id="admin",
    ).resource

    loaded = repository.load_active_configuration()

    assert loaded.providers == (provider,)
    assert loaded.logical_models == (model,)
    assert loaded.candidates == (candidate,)
    assert loaded.aliases == {"premium-chat": "gpt-5.5"}


def test_soft_deleted_providers_are_excluded_from_active_configuration(
    configuration_repository,
) -> None:
    repository, _pool = configuration_repository
    provider = repository.create_provider(
        name="temporary-vendor",
        protocol="gemini",
        base_url="https://temporary.example",
        credential="secret",
        actor_id="admin",
    ).resource

    deleted = repository.soft_delete_provider(provider.id, actor_id="admin")

    assert deleted.resource.status == "deleted"
    assert deleted.resource.deleted_at is not None
    assert repository.list_providers() == []
    assert repository.load_active_configuration().providers == ()


def test_provider_urls_reject_private_or_credential_bearing_destinations(
    configuration_repository,
) -> None:
    repository, _pool = configuration_repository

    for base_url in (
        "http://127.0.0.1:8080",
        "https://user:password@example.com",
        "https://example.com/path#fragment",
    ):
        with pytest.raises(ValueError, match="base URL"):
            repository.create_provider(
                name=f"invalid-{len(base_url)}",
                protocol="gemini",
                base_url=base_url,
                credential="secret",
                actor_id="admin",
            )


def test_model_and_candidate_updates_preserve_transactional_versions(
    configuration_repository,
) -> None:
    repository, _pool = configuration_repository
    provider = repository.create_provider(
        name="candidate-vendor",
        protocol="anthropic",
        base_url="https://candidate.example",
        credential="secret",
        actor_id="admin",
    ).resource
    model = repository.create_logical_model(
        model_id="portable-model",
        description="Initial",
        aliases=("old-alias",),
        actor_id="admin",
    ).resource
    candidate = repository.create_candidate(
        logical_model_id=model.id,
        provider_connection_id=provider.id,
        upstream_model_id="upstream-model",
        context_window_tokens=32_000,
        max_output_tokens=4_096,
        actor_id="admin",
    ).resource

    updated_model = repository.update_logical_model(
        model.id,
        description="Updated",
        aliases=("new-alias",),
        actor_id="admin",
    )
    updated_candidate = repository.update_candidate(
        candidate.id,
        priority=5,
        weight=7,
        pricing={"input_per_million": "0.50"},
        actor_id="admin",
    )
    deleted_candidate = repository.soft_delete_candidate(candidate.id, actor_id="admin")

    assert updated_model.resource.aliases == ("new-alias",)
    assert updated_model.resource.updated_at == updated_model.version.created_at
    assert updated_candidate.resource.priority == 5
    assert updated_candidate.resource.weight == 7
    assert updated_candidate.resource.updated_at == updated_candidate.version.created_at
    assert deleted_candidate.resource.status == "deleted"
    assert repository.list_candidates() == []


def test_outbox_claims_are_leased_and_owner_checked(configuration_repository) -> None:
    repository, pool = configuration_repository
    created = repository.create_provider(
        name="outbox-vendor",
        protocol="gemini",
        base_url="https://outbox.example",
        credential="secret",
        actor_id="admin",
    )

    claimed = repository.claim_outbox("publisher-a", limit=1, lease_seconds=60)

    assert [event.id for event in claimed] == [created.outbox_id]
    assert claimed[0].attempts == 1
    assert repository.claim_outbox("publisher-b", limit=1, lease_seconds=60) == []
    assert repository.renew_outbox_claim(created.outbox_id, "publisher-a") is True
    assert repository.complete_outbox(created.outbox_id, "publisher-b") is False
    assert repository.complete_outbox(created.outbox_id, "publisher-a") is True

    with pool.connection() as connection:
        version = connection.execute(
            "SELECT status, published_at FROM configuration_versions WHERE version = %s",
            (created.version.version,),
        ).fetchone()

    assert version["status"] == "published"
    assert version["published_at"] is not None
