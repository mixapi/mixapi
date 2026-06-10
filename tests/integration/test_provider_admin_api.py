from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.app_factory import create_app
from mixapi.configuration import ProviderConnection
from mixapi.provider_testing import ProviderConnectionTester, ProviderTestResult
from mixapi.secrets import CredentialAAD, CredentialCipher


ADMIN_HEADERS = {"Authorization": "Bearer admin-secret"}
MASTER_KEY = bytes.fromhex("00" * 32)


@pytest.fixture
def provider_client():
    app = create_app(admin_api_key="admin-secret")
    with TestClient(app) as client:
        yield client


def _create_provider(client: TestClient, **overrides):
    payload = {
        "name": "vendor-gemini-a",
        "protocol": "gemini",
        "base_url": "https://vendor.example",
        "credential": "provider-secret",
        "timeout_seconds": 12,
        "priority": 10,
        "weight": 2,
        "metadata": {"region": "us"},
    }
    payload.update(overrides)
    return client.post("/admin/v1/providers", headers=ADMIN_HEADERS, json=payload)


def test_provider_crud_returns_pending_versions_and_redacts_credentials(
    provider_client,
    database_url: str,
) -> None:
    missing_auth = provider_client.get("/admin/v1/providers")
    created = _create_provider(provider_client)

    assert missing_auth.status_code == 401
    assert created.status_code == 202
    body = created.json()
    provider_id = body["provider"]["id"]
    assert body["configuration_version"]["status"] == "pending"
    assert body["provider"]["credential_configured"] is True
    assert "provider-secret" not in json.dumps(body)
    assert "ciphertext" not in json.dumps(body)

    listed = provider_client.get("/admin/v1/providers", headers=ADMIN_HEADERS)
    fetched = provider_client.get(
        f"/admin/v1/providers/{provider_id}", headers=ADMIN_HEADERS
    )
    assert listed.status_code == 200
    assert any(item["id"] == provider_id for item in listed.json()["data"])
    assert fetched.status_code == 200
    assert fetched.json()["id"] == provider_id
    assert "credential" not in fetched.json()

    with psycopg.connect(database_url) as connection:
        audit = connection.execute(
            """
            SELECT action, after FROM audit_events
            WHERE target_id = %s ORDER BY sequence_id
            """,
            (provider_id,),
        ).fetchall()
    assert [row[0] for row in audit] == ["provider.created"]
    assert "provider-secret" not in str(audit[0][1])

    deleted = provider_client.delete(
        f"/admin/v1/providers/{provider_id}", headers=ADMIN_HEADERS
    )
    assert deleted.status_code == 202
    assert deleted.json()["provider"]["status"] == "deleted"
    assert provider_client.get(
        f"/admin/v1/providers/{provider_id}", headers=ADMIN_HEADERS
    ).status_code == 404


def test_provider_patch_rotates_credentials_and_enforces_optimistic_version(
    provider_client,
) -> None:
    created = _create_provider(provider_client).json()["provider"]
    provider_id = created["id"]
    original_fingerprint = created["credential_fingerprint"]
    updated = provider_client.patch(
        f"/admin/v1/providers/{provider_id}",
        headers=ADMIN_HEADERS,
        json={
            "expected_updated_at": created["updated_at"],
            "name": "vendor-gemini-renamed",
            "credential": "rotated-secret",
        },
    )

    assert updated.status_code == 202
    updated_body = updated.json()
    assert updated_body["provider"]["name"] == "vendor-gemini-renamed"
    assert updated_body["provider"]["credential_fingerprint"] != original_fingerprint
    assert "rotated-secret" not in json.dumps(updated_body)

    stale = provider_client.patch(
        f"/admin/v1/providers/{provider_id}",
        headers=ADMIN_HEADERS,
        json={
            "expected_updated_at": created["updated_at"],
            "priority": 1,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "configuration_conflict"


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"protocol": "unsupported"}, "invalid_provider_payload"),
        ({"base_url": "http://127.0.0.1:8000"}, "invalid_provider_payload"),
        ({"base_url": "https://user:pass@example.com"}, "invalid_provider_payload"),
    ],
)
def test_provider_validation_rejects_protocol_and_unsafe_urls(
    provider_client,
    overrides,
    expected_code,
) -> None:
    response = _create_provider(provider_client, **overrides)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == expected_code


def test_duplicate_provider_names_return_conflict(provider_client) -> None:
    assert _create_provider(provider_client).status_code == 202

    duplicate = _create_provider(provider_client, base_url="https://other.example")

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "provider_conflict"


def test_provider_test_endpoint_returns_only_normalized_probe_metadata() -> None:
    class StubTester:
        def test(self, _provider):
            return ProviderTestResult(status="error", latency_ms=17, error_class="timeout")

    app = create_app(admin_api_key="admin-secret", provider_tester=StubTester())
    with TestClient(app) as client:
        provider = _create_provider(client).json()["provider"]
        response = client.post(
            f"/admin/v1/providers/{provider['id']}/test",
            headers=ADMIN_HEADERS,
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "error",
        "latency_ms": 17,
        "error_class": "timeout",
    }


class _ProbeHandler(BaseHTTPRequestHandler):
    records: list[dict[str, object]] = []
    delay_seconds = 0.0

    def do_GET(self) -> None:
        type(self).records.append(
            {
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
            }
        )
        if type(self).delay_seconds:
            time.sleep(type(self).delay_seconds)
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, _format: str, *_args) -> None:
        return


@pytest.fixture
def probe_server():
    _ProbeHandler.records = []
    _ProbeHandler.delay_seconds = 0.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _ProbeHandler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def _connection(protocol: str, base_url: str) -> tuple[ProviderConnection, CredentialCipher]:
    cipher = CredentialCipher(MASTER_KEY, active_version=1)
    provider_id = f"provider_{protocol}"
    credential = cipher.encrypt(
        "probe-secret",
        CredentialAAD(provider_id=provider_id, protocol=protocol, field="api_key"),
    )
    now = datetime.now(UTC)
    return (
        ProviderConnection(
            id=provider_id,
            name=provider_id,
            protocol=protocol,
            base_url=base_url,
            credential=credential,
            timeout_seconds=Decimal("1"),
            status="active",
            priority=100,
            weight=1,
            metadata={},
            created_at=now,
            updated_at=now,
        ),
        cipher,
    )


@pytest.mark.parametrize(
    ("protocol", "path", "header"),
    [
        ("openai-compatible", "/models", ("authorization", "Bearer probe-secret")),
        ("anthropic", "/v1/models", ("x-api-key", "probe-secret")),
        ("gemini", "/v1beta/models", ("x-goog-api-key", "probe-secret")),
        ("ollama", "/api/tags", ("authorization", "Bearer probe-secret")),
    ],
)
def test_provider_probe_constructs_protocol_specific_requests(
    probe_server,
    protocol,
    path,
    header,
) -> None:
    base_url, handler = probe_server
    connection, cipher = _connection(protocol, base_url)
    tester = ProviderConnectionTester(cipher, max_timeout_seconds=1)

    result = tester.test(connection)

    assert result.status == "ok"
    assert result.error_class is None
    assert handler.records[-1]["path"] == path
    assert handler.records[-1]["headers"][header[0]] == header[1]


def test_provider_probe_timeout_is_bounded_and_normalized(probe_server) -> None:
    base_url, handler = probe_server
    handler.delay_seconds = 0.2
    connection, cipher = _connection("gemini", base_url)
    tester = ProviderConnectionTester(cipher, max_timeout_seconds=0.05)

    started = time.monotonic()
    result = tester.test(connection)
    elapsed = time.monotonic() - started

    assert result.status == "error"
    assert result.error_class == "timeout"
    assert elapsed < 0.5
