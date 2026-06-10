from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient

from mixapi.adapters import DeterministicProviderAdapter
from mixapi.app import create_app
from mixapi.postgres import DependencyUnavailable


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


def test_runtime_redis_outage_stops_public_data_plane_and_recovers() -> None:
    app = create_app()
    managed = app.state.control_plane.create_api_key(
        tenant_id="tenant_runtime",
        project_id="project_runtime",
        name="runtime key",
        scopes=("models:read", "responses:create", "embeddings:create"),
        model_allowlist=(),
        budget_limit_usd=Decimal("1"),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        actor_id="test",
    )

    with TestClient(app) as client:
        with (
            patch.object(
                app.state.redis_runtime,
                "ping",
                side_effect=DependencyUnavailable("Redis is unavailable"),
            ),
            patch.object(
                app.state.configuration_repository,
                "load_active_configuration",
            ) as load_configuration,
            patch.object(
                app.state.control_plane,
                "resolve_api_key",
            ) as resolve_api_key,
            patch.object(
                DeterministicProviderAdapter,
                "dispatch_response",
                autospec=True,
            ) as dispatch_response,
            patch.object(
                DeterministicProviderAdapter,
                "dispatch_embedding",
                autospec=True,
            ) as dispatch_embedding,
        ):
            responses = (
                client.get("/v1/models", headers=AUTH_HEADERS),
                client.post(
                    "/v1/responses",
                    headers=AUTH_HEADERS,
                    json={"model": "mixapi/balanced-chat", "input": "blocked"},
                ),
                client.post(
                    "/v1/embeddings",
                    headers=AUTH_HEADERS,
                    json={"model": "mixapi/embedding-small", "input": "blocked"},
                ),
                client.get(
                    "/v1/models",
                    headers={"Authorization": f"Bearer {managed.secret}"},
                ),
            )

            assert {response.status_code for response in responses} == {503}
            assert {
                response.json()["error"]["code"] for response in responses
            } == {"configuration_unavailable"}
            load_configuration.assert_not_called()
            resolve_api_key.assert_not_called()
            dispatch_response.assert_not_called()
            dispatch_embedding.assert_not_called()

        recovered = client.get("/v1/models", headers=AUTH_HEADERS)

    assert recovered.status_code == 200

