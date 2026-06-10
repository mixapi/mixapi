import json
import unittest
from decimal import Decimal

from fastapi.testclient import TestClient

from mixapi.adapters import ProviderStream, ProviderStreamEvent
from mixapi.app import _native_response_event_stream, create_app
from mixapi.auth import Principal
from mixapi.catalog import default_catalog
from mixapi.observability import InMemoryObservability
from mixapi.route_decisions import InMemoryRouteDecisionStore
from mixapi.streaming import _text_deltas
from mixapi.usage import InMemoryUsageLedger
from tests.runtime_fakes import FakeBudgetService, FakeCircuitBreaker, FakeQuotaService


AUTH_HEADERS = {"Authorization": "Bearer dev-key", "X-Request-ID": "req_stream_1"}


class StreamingTest(unittest.TestCase):
    def test_empty_output_text_emits_no_text_deltas(self) -> None:
        self.assertEqual(list(_text_deltas("")), [])

    def test_streaming_response_emits_ordered_normalized_events(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Stream this response",
                "routing": {"objective": "lowest-cost"},
                "stream": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        events = parse_sse(response.text)
        self.assertEqual(events[0]["event"], "response.created")
        self.assertEqual(events[-1]["event"], "response.completed")

        delta_events = [event for event in events if event["event"] == "response.output_text.delta"]
        self.assertGreaterEqual(len(delta_events), 1)
        streamed_text = "".join(event["data"]["delta"] for event in delta_events)
        completed = events[-1]["data"]["response"]
        self.assertEqual(streamed_text, completed["output_text"])
        self.assertEqual(completed["provider"], "ollama")
        self.assertEqual(completed["route"]["decision_trace_id"], "req_stream_1")
        self.assertEqual(len(app.state.usage_ledger.events()), 1)

    def test_streaming_response_persists_route_decision(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={"model": "mixapi/balanced-chat", "input": "Persist stream", "stream": True},
        )
        route_response = client.get("/v1/route-decisions/req_stream_1", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(route_response.status_code, 200)
        self.assertEqual(route_response.json()["status"], "succeeded")

    def test_streaming_fallback_completes_before_first_event(self) -> None:
        app = create_app(failed_response_providers={"ollama"})
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Fallback stream",
                "routing": {"objective": "lowest-cost"},
                "stream": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        events = parse_sse(response.text)
        self.assertEqual(events[0]["event"], "response.created")
        self.assertEqual(events[0]["data"]["provider"], "openai")
        completed = events[-1]["data"]["response"]
        self.assertEqual(completed["provider"], "openai")
        self.assertEqual(completed["route"]["attempts"], 2)
        self.assertTrue(completed["route"]["fallback_used"])

    def test_client_disconnect_closes_provider_and_finalizes_reservation(self) -> None:
        candidate = default_catalog()["mixapi/balanced-chat"].candidates[0]
        closed: list[bool] = []

        def provider_events():
            yield ProviderStreamEvent(delta="primed")
            yield ProviderStreamEvent(completed=True, input_tokens=1, output_tokens=1)

        provider_stream = ProviderStream(
            provider_events(),
            close=lambda: closed.append(True),
            candidate=candidate,
        )
        principal = Principal("tenant", "project", "key", ("responses:create",))
        budget = FakeBudgetService(limit_usd=Decimal("10"))
        reservation = budget.reserve(principal, Decimal("1"))
        quota = FakeQuotaService(token_limit=10)
        token_reservation = quota.reserve_tokens(principal, 8)
        usage = InMemoryUsageLedger()
        routes = InMemoryRouteDecisionStore()
        event_stream = _native_response_event_stream(
            provider_stream=provider_stream,
            selected_candidate=candidate,
            failed_attempts=[],
            rejected_candidates=(),
            request_body={"model": "mixapi/balanced-chat", "input": "disconnect"},
            request_id="req_disconnect",
            principal=principal,
            reservation=reservation,
            budget=budget,
            quota=quota,
            token_reservation=token_reservation,
            circuits=FakeCircuitBreaker(),
            usage_ledger=usage,
            route_decision_store=routes,
            observability=InMemoryObservability(),
            configuration_version=7,
            trace_id="trace_disconnect",
        )

        next(event_stream)
        event_stream.close()

        self.assertEqual(closed, [True])
        self.assertEqual(budget._reserved_spend["key"], Decimal("0"))
        self.assertEqual(quota._actual_tokens["key"], 1)
        self.assertEqual(quota._token_reservations, {})
        self.assertEqual(len(usage.events()), 1)
        self.assertEqual(usage.events()[0].configuration_version, 7)
        self.assertEqual(routes.get_public("req_disconnect", "tenant")["status"], "failed")
        self.assertEqual(routes._records[("tenant", "req_disconnect")].configuration_version, 7)


def parse_sse(raw: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for block in raw.strip().split("\n\n"):
        event_name = ""
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: "))
        events.append({"event": event_name, "data": json.loads("\n".join(data_lines))})
    return events


if __name__ == "__main__":
    unittest.main()
