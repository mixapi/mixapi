import unittest
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient

from mixapi.adapters import AdapterResponse, DeterministicProviderAdapter
from tests.app_factory import create_app
from mixapi.observability import InMemoryObservability, VersionedObservability


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


class ObservabilityCollectorTest(unittest.TestCase):
    def test_versioned_observability_stamps_spans_but_not_metric_labels(self) -> None:
        collector = InMemoryObservability()
        observability = VersionedObservability(collector, configuration_version=17)

        observability.increment_counter("requests", {"endpoint": "responses"})
        observability.record_span(
            "request",
            trace_id="trace_versioned",
            status="ok",
            duration_ms=1,
            attributes={"endpoint": "responses"},
        )

        self.assertEqual(
            dict(collector.counter_samples()[0].labels),
            {"endpoint": "responses"},
        )
        self.assertEqual(
            dict(collector.spans()[0].attributes),
            {"configuration_version": 17, "endpoint": "responses"},
        )

    def test_metric_snapshots_are_deterministic(self) -> None:
        observability = InMemoryObservability()

        observability.increment_counter(
            "mixapi_provider_errors_total",
            {"error_class": "upstream_timeout", "provider": "openai"},
        )
        observability.increment_counter(
            "mixapi_provider_errors_total",
            {"provider": "openai", "error_class": "upstream_timeout"},
            amount=2,
        )
        observability.observe_histogram(
            "mixapi_provider_latency_ms",
            12.5,
            {"provider_model": "gpt-4.1-mini", "provider": "openai"},
        )
        observability.set_gauge(
            "mixapi_circuit_state",
            1,
            {"provider_model": "gpt-4.1-mini", "provider": "openai"},
        )
        observability.set_gauge(
            "mixapi_circuit_state",
            0,
            {"provider": "openai", "provider_model": "gpt-4.1-mini"},
        )

        self.assertEqual(observability.counter_samples()[0].value, 3)
        self.assertEqual(
            observability.counter_samples()[0].labels,
            (("error_class", "upstream_timeout"), ("provider", "openai")),
        )
        self.assertEqual(observability.histogram_samples()[0].value, 12.5)
        self.assertEqual(observability.gauge_samples()[0].value, 0)

    def test_span_snapshot_preserves_trace_context_without_payloads(self) -> None:
        observability = InMemoryObservability()

        observability.record_span(
            "provider.dispatch",
            trace_id="trace_test",
            status="error",
            duration_ms=4.25,
            attributes={
                "provider": "openai",
                "provider_model": "gpt-4.1-mini",
                "error_class": "upstream_timeout",
            },
        )

        span = observability.spans()[0]
        self.assertEqual(span.name, "provider.dispatch")
        self.assertEqual(span.trace_id, "trace_test")
        self.assertEqual(span.status, "error")
        self.assertEqual(span.duration_ms, 4.25)
        self.assertNotIn("input", dict(span.attributes))

    def test_event_retention_is_bounded(self) -> None:
        observability = InMemoryObservability(max_records=2)

        for index in range(3):
            observability.observe_histogram("latency", index, {})
            observability.record_span(
                "provider.dispatch",
                trace_id=f"trace_{index}",
                status="ok",
                duration_ms=index,
                attributes={},
            )

        self.assertEqual(
            [sample.value for sample in observability.histogram_samples()],
            [1, 2],
        )
        self.assertEqual(
            [span.trace_id for span in observability.spans()],
            ["trace_1", "trace_2"],
        )


class ObservabilityEndpointTest(unittest.TestCase):
    def test_successful_response_records_provider_latency_and_trace_span(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers={**AUTH_HEADERS, "traceparent": "trace_provider_success"},
            json={
                "model": "mixapi/balanced-chat",
                "input": "Do not capture this prompt",
                "native": {"provider": "openai"},
            },
        )

        self.assertEqual(response.status_code, 200)
        latency = _metric_samples(app, "histogram", "mixapi_provider_latency_ms")
        self.assertEqual(len(latency), 1)
        self.assertEqual(
            dict(latency[0].labels),
            {"provider": "openai", "provider_model": "gpt-4.1-mini"},
        )
        spans = _spans(app, "provider.dispatch")
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].name, "provider.dispatch")
        self.assertEqual(spans[0].trace_id, "trace_provider_success")
        self.assertEqual(spans[0].status, "ok")
        self.assertNotIn("Do not capture this prompt", repr(spans[0]))

    def test_response_fallback_records_errors_attempts_and_transition(self) -> None:
        app = create_app(failed_response_providers={"ollama"})
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers={**AUTH_HEADERS, "traceparent": "trace_fallback"},
            json={
                "model": "mixapi/balanced-chat",
                "input": "Fallback metrics",
                "routing": {"objective": "lowest-cost"},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            len(_metric_samples(app, "histogram", "mixapi_provider_latency_ms")),
            2,
        )
        error = _metric_samples(app, "counter", "mixapi_provider_errors_total")[0]
        self.assertEqual(
            dict(error.labels),
            {"error_class": "configured_provider_failure", "provider": "ollama"},
        )
        fallback = _metric_samples(app, "counter", "mixapi_fallbacks_total")[0]
        self.assertEqual(
            dict(fallback.labels),
            {
                "from_provider": "ollama",
                "model": "mixapi/balanced-chat",
                "reason": "configured_provider_failure",
                "tenant": "tenant_dev",
                "to_provider": "openai",
            },
        )
        spans = _spans(app, "provider.dispatch")
        self.assertEqual([span.status for span in spans], ["error", "ok"])
        self.assertEqual({span.trace_id for span in spans}, {"trace_fallback"})
        fallback_span = _spans(app, "routing.fallback")[0]
        self.assertEqual(fallback_span.trace_id, "trace_fallback")
        self.assertEqual(dict(fallback_span.attributes)["from_provider"], "ollama")

    def test_embedding_records_provider_dispatch_span(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.post(
            "/v1/embeddings",
            headers={**AUTH_HEADERS, "traceparent": "trace_embedding"},
            json={
                "model": "mixapi/embedding-small",
                "input": "Embedding telemetry",
                "native": {"provider": "openai"},
            },
        )

        self.assertEqual(response.status_code, 200)
        span = _spans(app, "provider.dispatch")[0]
        self.assertEqual(dict(span.attributes)["endpoint"], "embeddings")
        self.assertEqual(span.trace_id, "trace_embedding")

    def test_streaming_fallback_records_both_provider_attempts(self) -> None:
        app = create_app(failed_response_providers={"ollama"})
        client = TestClient(app)

        with client.stream(
            "POST",
            "/v1/responses",
            headers={**AUTH_HEADERS, "traceparent": "trace_stream_fallback"},
            json={
                "model": "mixapi/balanced-chat",
                "input": "Stream fallback telemetry",
                "stream": True,
                "routing": {"objective": "lowest-cost"},
            },
        ) as response:
            list(response.iter_lines())

        self.assertEqual(response.status_code, 200)
        spans = _spans(app, "provider.dispatch")
        self.assertEqual([span.status for span in spans], ["error", "ok"])
        self.assertEqual(
            len(_metric_samples(app, "counter", "mixapi_fallbacks_total")),
            1,
        )

    def test_circuit_state_gauges_track_failed_and_successful_providers(self) -> None:
        app = create_app(
            failed_response_providers={"ollama"},
            circuit_failure_threshold=1,
        )
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Circuit telemetry",
                "routing": {"objective": "lowest-cost"},
            },
        )

        self.assertEqual(response.status_code, 200)
        gauges = {
            tuple(sample.labels): sample.value
            for sample in _metric_samples(app, "gauge", "mixapi_circuit_state")
        }
        self.assertEqual(
            gauges[
                (("provider", "ollama"), ("provider_model", "llama3.1"))
            ],
            1,
        )
        self.assertEqual(
            gauges[
                (("provider", "openai"), ("provider_model", "gpt-4.1-mini"))
            ],
            0,
        )
        circuit_spans = _spans(app, "circuit.state")
        self.assertTrue(
            any(
                dict(span.attributes).get("provider") == "ollama"
                and dict(span.attributes).get("state") == "open"
                for span in circuit_spans
            )
        )

    def test_open_circuit_rejection_uses_current_request_trace(self) -> None:
        app = create_app(
            failed_response_providers={"ollama"},
            circuit_failure_threshold=1,
        )
        client = TestClient(app)
        request_body = {
            "model": "mixapi/balanced-chat",
            "input": "Circuit rejection trace",
            "native": {"provider": "ollama"},
        }

        client.post("/v1/responses", headers=AUTH_HEADERS, json=request_body)
        response = client.post(
            "/v1/responses",
            headers={**AUTH_HEADERS, "traceparent": "trace_open_circuit"},
            json=request_body,
        )

        self.assertEqual(response.status_code, 503)
        spans = [
            span
            for span in _spans(app, "circuit.state")
            if span.trace_id == "trace_open_circuit"
        ]
        self.assertEqual(len(spans), 1)
        self.assertEqual(dict(spans[0].attributes)["state"], "open")

    def test_instrumentation_failure_does_not_change_response(self) -> None:
        app = create_app(observability=ExplodingObservability())
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Exporter failure",
                "native": {"provider": "openai"},
            },
        )

        self.assertEqual(response.status_code, 200)

    def test_request_budget_denial_records_metric_and_failed_span(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers={**AUTH_HEADERS, "traceparent": "trace_request_budget"},
            json={
                "model": "mixapi/balanced-chat",
                "input": "Budget telemetry",
                "max_output_tokens": 4,
                "routing": {"max_cost_usd": "0.000006"},
                "native": {"provider": "openai"},
            },
        )

        self.assertEqual(response.status_code, 402)
        denial = _metric_samples(app, "counter", "mixapi_budget_denials_total")[0]
        self.assertEqual(
            dict(denial.labels),
            {"project": "project_dev", "tenant": "tenant_dev"},
        )
        span = [span for span in app.state.observability.spans() if span.name == "budget.reserve"][0]
        self.assertEqual(span.trace_id, "trace_request_budget")
        self.assertEqual(span.status, "error")
        self.assertEqual(dict(span.attributes)["error_class"], "request_budget_exceeded")

    def test_api_key_budget_denial_records_metric_and_failed_span(self) -> None:
        app = create_app(budget_limit_usd=Decimal("0.00001000"))
        client = TestClient(app)
        request_body = {
            "model": "mixapi/balanced-chat",
            "input": "Budget telemetry",
            "max_output_tokens": 4,
            "native": {"provider": "openai"},
        }

        first = client.post("/v1/responses", headers=AUTH_HEADERS, json=request_body)
        second = client.post(
            "/v1/responses",
            headers={**AUTH_HEADERS, "traceparent": "trace_key_budget"},
            json=request_body,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 402)
        denials = _metric_samples(app, "counter", "mixapi_budget_denials_total")
        self.assertEqual(len(denials), 1)
        spans = [
            span
            for span in app.state.observability.spans()
            if span.name == "budget.reserve" and span.trace_id == "trace_key_budget"
        ]
        self.assertEqual(spans[0].status, "error")
        self.assertEqual(dict(spans[0].attributes)["error_class"], "api_key_budget_exceeded")

    def test_structured_output_validation_records_safe_spans_and_failure_counter(self) -> None:
        responses = iter(
            (
                AdapterResponse("private invalid generated output", 2, 3),
                AdapterResponse('{"ok":true}', 4, 5),
            )
        )
        app = create_app()
        client = TestClient(app)

        with patch.object(
            DeterministicProviderAdapter,
            "dispatch_response",
            autospec=True,
            side_effect=lambda *_args: next(responses),
        ):
            response = client.post(
                "/v1/responses",
                headers={**AUTH_HEADERS, "traceparent": "trace_structured"},
                json={
                    "model": "mixapi/balanced-chat",
                    "input": "private prompt text",
                    "native": {"provider": "openai"},
                    "response": {
                        "format": {
                            "type": "json_schema",
                            "json_schema": {
                                "type": "object",
                                "properties": {"ok": {"type": "boolean"}},
                                "required": ["ok"],
                            },
                        }
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        spans = _spans(app, "structured_output.validate")
        self.assertEqual([span.status for span in spans], ["error", "ok"])
        self.assertEqual([span.trace_id for span in spans], ["trace_structured"] * 2)
        self.assertEqual(
            [dict(span.attributes)["retry_number"] for span in spans],
            [0, 1],
        )
        self.assertEqual(
            dict(spans[0].attributes),
            {
                "configuration_version": app.state.snapshot_store.active_version(),
                "endpoint": "responses",
                "error_class": "schema_validation_failed",
                "provider": "openai",
                "provider_model": "gpt-4.1-mini",
                "retry_number": 0,
            },
        )
        counter = _metric_samples(
            app,
            "counter",
            "mixapi_structured_output_failures_total",
        )[0]
        self.assertEqual(counter.value, 1)
        self.assertEqual(
            dict(counter.labels),
            {"provider": "openai", "provider_model": "gpt-4.1-mini"},
        )
        telemetry = repr((*spans, counter))
        self.assertNotIn("private prompt text", telemetry)
        self.assertNotIn("private invalid generated output", telemetry)
        self.assertNotIn("properties", telemetry)


def _metric_samples(app, kind: str, name: str):
    samples = getattr(app.state.observability, f"{kind}_samples")()
    return [sample for sample in samples if sample.name == name]


def _spans(app, name: str):
    return [span for span in app.state.observability.spans() if span.name == name]


class ExplodingObservability:
    def increment_counter(self, *args, **kwargs) -> None:
        raise RuntimeError("collector unavailable")

    def observe_histogram(self, *args, **kwargs) -> None:
        raise RuntimeError("collector unavailable")

    def set_gauge(self, *args, **kwargs) -> None:
        raise RuntimeError("collector unavailable")

    def record_span(self, *args, **kwargs) -> None:
        raise RuntimeError("collector unavailable")


if __name__ == "__main__":
    unittest.main()
