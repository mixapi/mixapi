from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.app_factory import create_app


ADMIN_HEADERS = {"Authorization": "Bearer admin-secret"}


@pytest.fixture
def model_client():
    app = create_app(admin_api_key="admin-secret")
    with TestClient(app) as client:
        yield client


def _create_model(client: TestClient, **overrides):
    payload = {
        "id": "gpt-5.5",
        "description": "Portable multi-vendor model",
        "aliases": ["reasoning-latest"],
        "status": "disabled",
    }
    payload.update(overrides)
    return client.post("/admin/v1/models", headers=ADMIN_HEADERS, json=payload)


def _candidate_payload(provider_id: str, upstream_model: str, **overrides):
    payload = {
        "logical_model_id": "gpt-5.5",
        "provider_connection_id": provider_id,
        "upstream_model_id": upstream_model,
        "status": "active",
        "priority": 10,
        "weight": 1,
        "context_window_tokens": 200000,
        "max_output_tokens": 32000,
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "tool_modes": ["function"],
        "schema_support": "best_effort_schema",
        "streaming_support": True,
        "embeddings_support": False,
        "retention_class": "standard",
        "regions": ["us"],
        "pricing": {"input_per_million": "1.00"},
        "native_features": [],
        "unsupported_parameters": [],
    }
    payload.update(overrides)
    return payload


def test_logical_model_requires_candidates_before_activation_and_aliases_are_unique(
    model_client,
) -> None:
    rejected = _create_model(model_client, id="active-empty", aliases=[], status="active")
    created = _create_model(model_client)

    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "active_model_requires_candidate"
    assert created.status_code == 202
    model = created.json()["logical_model"]
    assert model["aliases"] == ["reasoning-latest"]
    assert created.json()["configuration_version"]["status"] == "pending"

    duplicate_alias = _create_model(
        model_client,
        id="other-model",
        aliases=["reasoning-latest"],
    )
    assert duplicate_alias.status_code == 409
    assert duplicate_alias.json()["error"]["code"] == "model_conflict"

    activation = model_client.patch(
        "/admin/v1/models/gpt-5.5",
        headers=ADMIN_HEADERS,
        json={"expected_updated_at": model["updated_at"], "status": "active"},
    )
    assert activation.status_code == 400
    assert activation.json()["error"]["code"] == "active_model_requires_candidate"


def test_model_maps_to_gemini_anthropic_and_openai_compatible_connections(
    model_client,
) -> None:
    model = _create_model(model_client).json()["logical_model"]
    mappings = (
        ("provider_gemini", "gemini-2.5-pro"),
        ("provider_anthropic", "claude-sonnet-4"),
        ("provider_openai", "vendor/gpt-compatible"),
    )
    candidates = []
    for index, (provider_id, upstream_model) in enumerate(mappings):
        response = model_client.post(
            "/admin/v1/candidates",
            headers=ADMIN_HEADERS,
            json=_candidate_payload(
                provider_id,
                upstream_model,
                priority=(index + 1) * 10,
            ),
        )
        assert response.status_code == 202
        candidates.append(response.json()["candidate"])

    activated = model_client.patch(
        "/admin/v1/models/gpt-5.5",
        headers=ADMIN_HEADERS,
        json={"expected_updated_at": model["updated_at"], "status": "active"},
    )
    listed = model_client.get("/admin/v1/candidates", headers=ADMIN_HEADERS)

    assert activated.status_code == 202
    assert activated.json()["logical_model"]["status"] == "active"
    selected = [
        item for item in listed.json()["data"] if item["logical_model_id"] == "gpt-5.5"
    ]
    assert {
        (item["provider_connection_id"], item["upstream_model_id"])
        for item in selected
    } == set(mappings)
    assert all("credential" not in item for item in selected)


def test_candidate_validation_and_last_candidate_protection(model_client) -> None:
    model = _create_model(model_client).json()["logical_model"]
    missing_provider = model_client.post(
        "/admin/v1/candidates",
        headers=ADMIN_HEADERS,
        json=_candidate_payload("missing-provider", "model"),
    )
    assert missing_provider.status_code == 400
    assert missing_provider.json()["error"]["code"] == "invalid_candidate_reference"

    created = model_client.post(
        "/admin/v1/candidates",
        headers=ADMIN_HEADERS,
        json=_candidate_payload("provider_gemini", "gemini-2.5-pro"),
    ).json()["candidate"]
    activated = model_client.patch(
        "/admin/v1/models/gpt-5.5",
        headers=ADMIN_HEADERS,
        json={"expected_updated_at": model["updated_at"], "status": "active"},
    )
    assert activated.status_code == 202

    rejected = model_client.delete(
        f"/admin/v1/candidates/{created['id']}",
        headers=ADMIN_HEADERS,
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "active_model_requires_candidate"


def test_model_and_candidate_updates_are_optimistic_and_soft_delete(model_client) -> None:
    model = _create_model(model_client).json()["logical_model"]
    candidate = model_client.post(
        "/admin/v1/candidates",
        headers=ADMIN_HEADERS,
        json=_candidate_payload("provider_gemini", "gemini-2.5-pro"),
    ).json()["candidate"]

    updated = model_client.patch(
        f"/admin/v1/candidates/{candidate['id']}",
        headers=ADMIN_HEADERS,
        json={"expected_updated_at": candidate["updated_at"], "weight": 4},
    )
    stale = model_client.patch(
        f"/admin/v1/candidates/{candidate['id']}",
        headers=ADMIN_HEADERS,
        json={"expected_updated_at": candidate["updated_at"], "priority": 1},
    )
    deleted_model = model_client.delete(
        "/admin/v1/models/gpt-5.5", headers=ADMIN_HEADERS
    )

    assert updated.status_code == 202
    assert updated.json()["candidate"]["weight"] == 4
    assert stale.status_code == 409
    assert deleted_model.status_code == 202
    assert model_client.get(
        "/admin/v1/models/gpt-5.5", headers=ADMIN_HEADERS
    ).status_code == 404
