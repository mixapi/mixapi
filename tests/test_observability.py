import unittest

from fastapi.testclient import TestClient

from mixapi.app import create_app
from mixapi.observability import InMemoryObservability


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


class ObservabilityCollectorTest(unittest.TestCase):
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
        spans = app.state.observability.spans()
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
        spans = app.state.observability.spans()
        self.assertEqual([span.status for span in spans], ["error", "ok"])
        self.assertEqual({span.trace_id for span in spans}, {"trace_fallback"})

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
        span = app.state.observability.spans()[0]
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
        spans = app.state.observability.spans()
        self.assertEqual([span.status for span in spans], ["error", "ok"])
        self.assertEqual(
            len(_metric_samples(app, "counter", "mixapi_fallbacks_total")),
            1,
        )


def _metric_samples(app, kind: str, name: str):
    samples = getattr(app.state.observability, f"{kind}_samples")()
    return [sample for sample in samples if sample.name == name]


if __name__ == "__main__":
    unittest.main()
