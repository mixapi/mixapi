import unittest

from mixapi.observability import InMemoryObservability


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


if __name__ == "__main__":
    unittest.main()
