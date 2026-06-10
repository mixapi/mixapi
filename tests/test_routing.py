from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from mixapi.configuration import LogicalModel as StoredLogicalModel
from mixapi.configuration import ModelCandidate, ProviderConnection, StoredConfiguration
from mixapi.models import LogicalModel, ProviderModel
from mixapi.routing import plan_route
from mixapi.secrets import EncryptedCredential
from mixapi.snapshots import ConfigurationSnapshot


def _candidate(name: str, *, priority: int = 10, weight: int = 1, cost: str = "1"):
    return ProviderModel(
        id=f"candidate_{name}",
        provider=name,
        provider_model_id=f"upstream-{name}",
        logical_model_id="gpt-5.5",
        status="active",
        context_window_tokens=1000,
        max_output_tokens=100,
        input_modalities=("text",),
        output_modalities=("text",),
        tool_modes=("function",),
        schema_support="strict_json_schema",
        streaming_support=True,
        embeddings_support=False,
        retention_class="standard",
        regions=("us",),
        pricing={"input_per_million": cost, "output_per_million": cost},
        provider_connection_id=f"provider_{name}",
        provider_connection_name=name,
        protocol="gemini",
        priority=priority,
        weight=weight,
    )


def test_balanced_routing_uses_lowest_priority_then_weighted_rendezvous() -> None:
    catalog = {
        "gpt-5.5": LogicalModel(
            id="gpt-5.5",
            status="active",
            description="",
            candidates=(
                _candidate("a", priority=10, weight=1),
                _candidate("b", priority=10, weight=5),
                _candidate("c", priority=20, weight=100),
            ),
        )
    }

    selections = [
        plan_route(
            catalog,
            {"model": "gpt-5.5", "input": "x"},
            "responses",
            selection_seed=f"request-{index}",
        ).candidate.provider
        for index in range(40)
    ]

    assert "c" not in selections
    assert selections.count("b") > selections.count("a")
    assert plan_route(
        catalog,
        {"model": "gpt-5.5", "input": "x"},
        "responses",
        selection_seed="stable",
    ).candidate == plan_route(
        catalog,
        {"model": "gpt-5.5", "input": "x"},
        "responses",
        selection_seed="stable",
    ).candidate


def test_explicit_objective_and_connection_pin_override_balanced_priority() -> None:
    cheap = _candidate("cheap", priority=50, cost="0.1")
    preferred = _candidate("preferred", priority=10, cost="10")
    catalog = {
        "gpt-5.5": LogicalModel("gpt-5.5", "active", "", (preferred, cheap))
    }

    lowest_cost = plan_route(
        catalog,
        {"model": "gpt-5.5", "input": "x", "routing": {"objective": "lowest-cost"}},
        "responses",
        selection_seed="seed",
    )
    pinned = plan_route(
        catalog,
        {"model": "gpt-5.5", "input": "x", "native": {"provider": "provider_cheap"}},
        "responses",
        selection_seed="seed",
    )

    assert lowest_cost.candidate.provider == "cheap"
    assert pinned.candidate.provider_connection_id == "provider_cheap"


def test_snapshot_alias_resolves_to_canonical_allowlisted_model() -> None:
    now = datetime(2026, 6, 10, tzinfo=UTC)
    credential = EncryptedCredential(1, b"0123456789ab", b"ciphertext", "fingerprint")
    provider = ProviderConnection(
        "provider_a", "vendor-a", "gemini", "https://a.example", credential,
        Decimal("30"), "active", 10, 1, {}, now, now,
    )
    model = StoredLogicalModel("gpt-5.5", "", "active", ("reasoning-latest",), now, now)
    candidate = ModelCandidate(
        "candidate_a", "gpt-5.5", "provider_a", "gemini-test", "active", 10, 1,
        1000, 100, ("text",), ("text",), (), "none", True, False, "standard",
        ("us",), {}, (), (), now, now,
    )
    snapshot = ConfigurationSnapshot.create(
        1,
        now,
        StoredConfiguration((provider,), (model,), (candidate,), {"reasoning-latest": "gpt-5.5"}),
    )

    decision = plan_route(
        snapshot,
        {"model": "reasoning-latest", "input": "x"},
        "responses",
        model_allowlist=("gpt-5.5",),
        selection_seed="seed",
    )

    assert decision.logical_model_id == "gpt-5.5"
    assert decision.candidate.provider == "vendor-a"
