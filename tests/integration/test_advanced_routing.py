from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from tests.app_factory import create_app
from mixapi.configuration import LogicalModel, ModelCandidate, ProviderConnection, StoredConfiguration
from mixapi.secrets import CredentialAAD, CredentialCipher
from mixapi.snapshots import ConfigurationSnapshot


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


class _GeminiHandler(BaseHTTPRequestHandler):
    name = ""
    status = 200
    records: list[str] = []

    def do_POST(self) -> None:
        type(self).records.append(self.headers.get("x-goog-api-key", ""))
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        self.send_response(type(self).status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        if type(self).status < 400:
            self.wfile.write(
                json.dumps(
                    {
                        "candidates": [{"content": {"parts": [{"text": type(self).name}]}}],
                        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
                    }
                ).encode()
            )
        else:
            self.wfile.write(b'{}')

    def log_message(self, _format: str, *_args) -> None:
        return


def _server(name: str, status: int = 200):
    handler = type(f"{name}Handler", (_GeminiHandler,), {"name": name, "status": status, "records": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, handler


def _activate(app, endpoints, priorities, metadata=None) -> int:
    settings = app.state.settings
    cipher = CredentialCipher(settings.master_key, active_version=settings.master_key_version)
    now = datetime.now(UTC)
    providers = []
    candidates = []
    metadata = metadata or {}
    for name, base_url, secret in endpoints:
        provider_id = f"provider_{name}"
        providers.append(
            ProviderConnection(
                provider_id, name, "gemini", base_url,
                cipher.encrypt(secret, CredentialAAD(provider_id, "gemini", "api_key")),
                Decimal("2"), "active", priorities[name], 1, metadata.get(name, {}), now, now,
            )
        )
        candidates.append(
            ModelCandidate(
                f"candidate_{name}", "gpt-5.5", provider_id, "gemini-test", "active",
                priorities[name], 1, 1000, 100, ("text",), ("text",), (), "none", True,
                False, "standard", ("us",), {"input_per_million": "1"}, (), (), now, now,
            )
        )
    model = LogicalModel("gpt-5.5", "dynamic", "active", (), now, now)
    version = (app.state.snapshot_store.active_version() or 0) + 100
    snapshot = ConfigurationSnapshot.create(
        version,
        now,
        StoredConfiguration(tuple(providers), (model,), tuple(candidates), {}),
    )
    store = app.state.snapshot_store
    store.write_pending(snapshot)
    store.mark_ready(version, snapshot.checksum)
    store.activate(version, snapshot.checksum)
    return version


def test_sticky_sessions_pin_connection() -> None:
    first_server, first_thread, _first_handler = _server("vendor-a")
    second_server, second_thread, _second_handler = _server("vendor-b")
    app = create_app()
    try:
        with TestClient(app) as client:
            _activate(
                app,
                (
                    ("vendor-a", f"http://127.0.0.1:{first_server.server_port}", "secret-a"),
                    ("vendor-b", f"http://127.0.0.1:{second_server.server_port}", "secret-b"),
                ),
                {"vendor-a": 10, "vendor-b": 10},
            )
            
            # Initial request with session ID
            headers = {**AUTH_HEADERS, "x-mixapi-session-id": "session-123"}
            response1 = client.post(
                "/v1/responses",
                headers=headers,
                json={"model": "gpt-5.5", "input": "hello"},
            )
            assert response1.status_code == 200
            provider1 = response1.json()["provider"]
            
            # Subsequent requests with same session ID should use same provider
            for i in range(5):
                response2 = client.post(
                    "/v1/responses",
                    headers={**headers, "X-Request-ID": f"req-{i}"},
                    json={"model": "gpt-5.5", "input": "hello"},
                )
                assert response2.status_code == 200
                assert response2.json()["provider"] == provider1
    finally:
        first_server.shutdown()
        first_server.server_close()
        first_thread.join(timeout=1)
        second_server.shutdown()
        second_server.server_close()
        second_thread.join(timeout=1)


def test_concurrency_limits_trigger_rotation() -> None:
    first_server, first_thread, _first_handler = _server("vendor-a")
    second_server, second_thread, _second_handler = _server("vendor-b")
    app = create_app()
    try:
        with TestClient(app) as client:
            # Set concurrency limit to 1 for vendor-a
            _activate(
                app,
                (
                    ("vendor-a", f"http://127.0.0.1:{first_server.server_port}", "secret-a"),
                    ("vendor-b", f"http://127.0.0.1:{second_server.server_port}", "secret-b"),
                ),
                {"vendor-a": 10, "vendor-b": 20},
                metadata={"vendor-a": {"concurrency_limit": 1}}
            )
            
            # Manually occupy a concurrency slot for vendor-a in Redis
            conn_id = "provider_vendor-a"
            key = f"{app.state.settings.redis_namespace}:v1:concurrency:{conn_id}"
            app.state.quota._client.set(key, 1)
            
            # Request should skip vendor-a and use vendor-b
            response = client.post(
                "/v1/responses",
                headers={**AUTH_HEADERS, "X-Request-ID": "req_concurrent_rotation"},
                json={"model": "gpt-5.5", "input": "hello"},
            )
            
            assert response.status_code == 200
            assert response.json()["provider"] == "vendor-b"
            assert response.json()["route"]["attempts"] == 2 # skipped a (attempt 1), tried b (attempt 2)
            
            # Clear slot, request should use vendor-a again
            app.state.quota._client.delete(key)
            response2 = client.post(
                "/v1/responses",
                headers={**AUTH_HEADERS, "X-Request-ID": "req_concurrent_available"},
                json={"model": "gpt-5.5", "input": "hello"},
            )
            assert response2.status_code == 200
            assert response2.json()["provider"] == "vendor-a"
    finally:
        first_server.shutdown()
        first_server.server_close()
        first_thread.join(timeout=1)
        second_server.shutdown()
        second_server.server_close()
        second_thread.join(timeout=1)


def test_request_quotas_trigger_rotation() -> None:
    first_server, first_thread, _first_handler = _server("vendor-a")
    second_server, second_thread, _second_handler = _server("vendor-b")
    app = create_app()
    try:
        with TestClient(app) as client:
            # Set request quota to 5 for vendor-a
            _activate(
                app,
                (
                    ("vendor-a", f"http://127.0.0.1:{first_server.server_port}", "secret-a"),
                    ("vendor-b", f"http://127.0.0.1:{second_server.server_port}", "secret-b"),
                ),
                {"vendor-a": 10, "vendor-b": 20},
                metadata={"vendor-a": {"request_quota": 5}}
            )
            
            # Manually exhaust quota for vendor-a in Redis
            conn_id = "provider_vendor-a"
            key = f"{app.state.settings.redis_namespace}:v1:quota:connection_requests:{conn_id}"
            app.state.quota._client.set(key, 5)
            
            # Request should skip vendor-a and use vendor-b
            response = client.post(
                "/v1/responses",
                headers={**AUTH_HEADERS, "X-Request-ID": "req_quota_rotation"},
                json={"model": "gpt-5.5", "input": "hello"},
            )
            
            assert response.status_code == 200
            assert response.json()["provider"] == "vendor-b"
            
            # Clear quota, request should use vendor-a
            app.state.quota._client.delete(key)
            response2 = client.post(
                "/v1/responses",
                headers={**AUTH_HEADERS, "X-Request-ID": "req_quota_available"},
                json={"model": "gpt-5.5", "input": "hello"},
            )
            assert response2.status_code == 200
            assert response2.json()["provider"] == "vendor-a"
    finally:
        first_server.shutdown()
        first_server.server_close()
        first_thread.join(timeout=1)
        second_server.shutdown()
        second_server.server_close()
        second_thread.join(timeout=1)
