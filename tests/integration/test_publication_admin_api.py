from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mixapi.app import create_app
from mixapi.publication import PublicationError


ADMIN_HEADERS = {"Authorization": "Bearer admin-secret"}


@pytest.fixture
def publication_client():
    app = create_app(admin_api_key="admin-secret")
    with TestClient(app) as client:
        yield client, app


def test_configuration_version_status_is_exposed_with_redacted_failures(
    publication_client,
) -> None:
    client, app = publication_client
    mutation = app.state.configuration_repository.create_logical_model(
        model_id="invalid-active-model",
        description="No candidates",
        aliases=(),
        status="active",
        actor_id="test",
    )
    with pytest.raises(PublicationError):
        app.state.configuration_publisher.publish(mutation.version.version)

    response = client.get(
        f"/admin/v1/configuration/versions/{mutation.version.version}",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["error_code"] == "missing_model_candidate"
    assert "ciphertext" not in str(response.json()).lower()
    assert "secret" not in str(response.json()).lower()


def test_manual_rebuild_repairs_snapshot_and_uses_lock(publication_client) -> None:
    client, app = publication_client
    version = app.state.snapshot_store.active_version()
    assert version is not None
    redis_client = app.state.redis_runtime.client
    namespace = app.state.settings.redis_namespace
    redis_client.delete(
        f"{namespace}:v1:snapshots:{version}:payload",
        f"{namespace}:v1:snapshots:{version}:manifest",
    )

    rebuilt = client.post(
        "/admin/v1/configuration/rebuild",
        headers=ADMIN_HEADERS,
    )
    assert rebuilt.status_code == 202
    assert rebuilt.json()["active_version"] == version
    assert app.state.snapshot_store.load(version).version == version

    assert app.state.snapshot_store.acquire_rebuild_lock("other", ttl_seconds=30)
    try:
        locked = client.post(
            "/admin/v1/configuration/rebuild",
            headers=ADMIN_HEADERS,
        )
    finally:
        app.state.snapshot_store.release_rebuild_lock("other")
    assert locked.status_code == 409
    assert locked.json()["error"]["code"] == "rebuild_in_progress"
