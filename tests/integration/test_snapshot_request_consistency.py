from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

from mixapi.adapters import DeterministicProviderAdapter
from mixapi.app import create_app
from mixapi.configuration import StoredConfiguration
from mixapi.snapshots import ConfigurationSnapshot


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


def _activate_renamed_snapshot(app, source: ConfigurationSnapshot) -> int:
    version = source.version + 100
    providers = tuple(
        replace(provider, name="openai-new", updated_at=datetime.now(UTC))
        if provider.id == "provider_openai"
        else provider
        for provider in source.providers
    )
    snapshot = ConfigurationSnapshot.create(
        version,
        datetime.now(UTC),
        StoredConfiguration(
            providers=providers,
            logical_models=source.logical_models,
            candidates=source.candidates,
            aliases=source.aliases,
        ),
    )
    store = app.state.snapshot_store
    store.write_pending(snapshot)
    store.mark_ready(version, snapshot.checksum)
    store.activate(version, snapshot.checksum)
    return version


def _recorded_versions(app, request_id: str) -> tuple[int, int, int]:
    with app.state.postgres_pool.connection() as connection:
        usage = connection.execute(
            "SELECT configuration_version FROM usage_events WHERE request_id = %s",
            (request_id,),
        ).fetchone()
        route = connection.execute(
            "SELECT configuration_version FROM route_decisions WHERE request_id = %s",
            (request_id,),
        ).fetchone()
        intent = connection.execute(
            "SELECT configuration_version FROM usage_write_intents WHERE request_id = %s",
            (request_id,),
        ).fetchone()
    assert usage is not None and route is not None and intent is not None
    return (
        usage["configuration_version"],
        route["configuration_version"],
        intent["configuration_version"],
    )


def test_request_keeps_one_snapshot_version_after_new_activation() -> None:
    app = create_app(failed_response_providers={"ollama"})
    original_dispatch = DeterministicProviderAdapter.dispatch_response

    with TestClient(app) as client:
        source = app.state.snapshot_store.load(app.state.snapshot_store.active_version())
        activated: list[int] = []

        def dispatch(adapter, request_body, candidate):
            if not activated:
                activated.append(_activate_renamed_snapshot(app, source))
            return original_dispatch(adapter, request_body, candidate)

        with patch.object(
            DeterministicProviderAdapter,
            "dispatch_response",
            autospec=True,
            side_effect=dispatch,
        ):
            response = client.post(
                "/v1/responses",
                headers={
                    **AUTH_HEADERS,
                    "X-Request-ID": "req_snapshot_consistent",
                    "traceparent": "trace_snapshot_consistent",
                },
                json={
                    "model": "mixapi/balanced-chat",
                    "input": "pin this request",
                    "routing": {"objective": "lowest-cost"},
                },
            )
        recorded_versions = _recorded_versions(app, "req_snapshot_consistent")
        request_spans = [
            span
            for span in app.state.observability.spans()
            if span.trace_id == "trace_snapshot_consistent"
        ]

    assert response.status_code == 200
    assert response.json()["provider"] == "openai"
    assert response.json()["route"]["fallback_used"] is True
    assert activated == [source.version + 100]
    assert recorded_versions == (source.version,) * 3
    assert {span.name for span in request_spans} >= {
        "budget.reserve",
        "provider.dispatch",
        "routing.fallback",
    }
    assert {
        dict(span.attributes)["configuration_version"] for span in request_spans
    } == {source.version}


def test_stream_completion_records_the_snapshot_selected_before_activation() -> None:
    app = create_app()
    original_start = DeterministicProviderAdapter.start_response_stream

    with TestClient(app) as client:
        source = app.state.snapshot_store.load(app.state.snapshot_store.active_version())

        def start(adapter, request_body, candidate):
            stream = original_start(adapter, request_body, candidate)
            _activate_renamed_snapshot(app, source)
            return stream

        with patch.object(
            DeterministicProviderAdapter,
            "start_response_stream",
            autospec=True,
            side_effect=start,
        ):
            response = client.post(
                "/v1/responses",
                headers={**AUTH_HEADERS, "X-Request-ID": "req_stream_snapshot"},
                json={
                    "model": "mixapi/balanced-chat",
                    "input": "stream with pinned configuration",
                    "native": {"provider": "openai"},
                    "stream": True,
                },
            )
        recorded_versions = _recorded_versions(app, "req_stream_snapshot")

    assert response.status_code == 200
    assert recorded_versions == (source.version,) * 3
