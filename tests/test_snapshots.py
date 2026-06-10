from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from mixapi.configuration import (
    LogicalModel,
    ModelCandidate,
    ProviderConnection,
    StoredConfiguration,
)
from mixapi.secrets import EncryptedCredential
from mixapi.snapshots import ConfigurationSnapshot, SnapshotValidationError


GENERATED_AT = datetime(2026, 6, 10, 12, 30, tzinfo=UTC)


def _configuration(*, reverse: bool = False) -> StoredConfiguration:
    providers = (
        ProviderConnection(
            id="provider_gemini",
            name="Gemini primary",
            protocol="gemini",
            base_url="https://generativelanguage.googleapis.com",
            credential=EncryptedCredential(
                key_version=2,
                nonce=b"0123456789ab",
                ciphertext=b"encrypted-gemini-key",
                fingerprint="gemini-fp",
            ),
            timeout_seconds=Decimal("30.500"),
            status="active",
            priority=10,
            weight=3,
            metadata={"region": "us", "cost_multiplier": Decimal("1.25")},
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 2, 1, tzinfo=UTC),
        ),
        ProviderConnection(
            id="provider_anthropic",
            name="Anthropic fallback",
            protocol="anthropic",
            base_url="https://api.anthropic.com",
            credential=EncryptedCredential(
                key_version=1,
                nonce=b"abcdefghijkl",
                ciphertext=b"encrypted-anthropic-key",
                fingerprint="anthropic-fp",
            ),
            timeout_seconds=Decimal("45"),
            status="active",
            priority=20,
            weight=1,
            metadata={},
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
            updated_at=datetime(2026, 2, 2, tzinfo=UTC),
        ),
    )
    models = (
        LogicalModel(
            id="gpt-5.5",
            description="Portable reasoning model",
            status="active",
            aliases=("reasoning-latest", "gpt-latest"),
            created_at=datetime(2026, 1, 3, tzinfo=UTC),
            updated_at=datetime(2026, 2, 3, tzinfo=UTC),
        ),
    )
    candidates = (
        ModelCandidate(
            id="candidate_gemini",
            logical_model_id="gpt-5.5",
            provider_connection_id="provider_gemini",
            upstream_model_id="gemini-2.5-pro",
            status="active",
            priority=10,
            weight=3,
            context_window_tokens=1_000_000,
            max_output_tokens=65_536,
            input_modalities=("text", "image"),
            output_modalities=("text",),
            tool_modes=("auto", "required"),
            schema_support="strict_json_schema",
            streaming_support=True,
            embeddings_support=False,
            retention_class="standard",
            regions=("us", "eu"),
            pricing={"input_per_million": Decimal("1.25000000")},
            native_features=("thinking",),
            unsupported_parameters=("logprobs",),
            created_at=datetime(2026, 1, 4, tzinfo=UTC),
            updated_at=datetime(2026, 2, 4, tzinfo=UTC),
        ),
        ModelCandidate(
            id="candidate_anthropic",
            logical_model_id="gpt-5.5",
            provider_connection_id="provider_anthropic",
            upstream_model_id="claude-opus",
            status="active",
            priority=20,
            weight=1,
            context_window_tokens=200_000,
            max_output_tokens=32_000,
            input_modalities=("text",),
            output_modalities=("text",),
            tool_modes=("auto",),
            schema_support="best_effort_schema",
            streaming_support=True,
            embeddings_support=False,
            retention_class="zero-retention",
            regions=("us",),
            pricing={"input_per_million": "15.00000000"},
            native_features=(),
            unsupported_parameters=(),
            created_at=datetime(2026, 1, 5, tzinfo=UTC),
            updated_at=datetime(2026, 2, 5, tzinfo=UTC),
        ),
    )
    if reverse:
        providers = tuple(reversed(providers))
        candidates = tuple(reversed(candidates))
    return StoredConfiguration(
        providers=providers,
        logical_models=models,
        candidates=candidates,
        aliases={"reasoning-latest": "gpt-5.5", "gpt-latest": "gpt-5.5"},
    )


def test_snapshot_serialization_is_deterministic_and_checksum_stable() -> None:
    first = ConfigurationSnapshot.create(7, GENERATED_AT, _configuration())
    second = ConfigurationSnapshot.create(7, GENERATED_AT, _configuration(reverse=True))

    assert first.to_json() == second.to_json()
    assert first.checksum == second.checksum
    assert '"timeout_seconds":"30.500"' in first.to_json()
    assert '"input_per_million":"1.25000000"' in first.to_json()


def test_snapshot_round_trip_preserves_complete_graph_and_encrypted_credentials() -> None:
    original = ConfigurationSnapshot.create(7, GENERATED_AT, _configuration())

    decoded = ConfigurationSnapshot.from_json(original.to_json())

    assert decoded == original
    assert decoded.resolve_model_id("reasoning-latest") == "gpt-5.5"
    assert decoded.resolve_model_id("gpt-5.5") == "gpt-5.5"
    assert decoded.resolve_model_id("unknown") is None
    assert decoded.providers[1].credential == EncryptedCredential(
        key_version=2,
        nonce=b"0123456789ab",
        ciphertext=b"encrypted-gemini-key",
        fingerprint="gemini-fp",
    )
    assert decoded.candidates[0].provider_connection_id == "provider_anthropic"
    assert decoded.candidates[1].unsupported_parameters == ("logprobs",)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"schema_version": 99}),
        lambda payload: payload.update({"unknown": True}),
        lambda payload: payload["providers"][0].update({"plaintext_api_key": "secret"}),
        lambda payload: payload.update({"checksum": "0" * 64}),
    ],
)
def test_snapshot_rejects_unsupported_schema_unknown_fields_and_bad_checksum(mutate) -> None:
    payload = json.loads(
        ConfigurationSnapshot.create(7, GENERATED_AT, _configuration()).to_json()
    )
    mutate(payload)

    with pytest.raises(SnapshotValidationError):
        ConfigurationSnapshot.from_json(json.dumps(payload))
