import os
import unittest

from fastapi.testclient import TestClient

from mixapi.app import create_app
from mixapi.circuits import InMemoryCircuitBreaker


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


class CircuitBreakerTest(unittest.TestCase):
    def test_open_circuit_allows_requests_after_recovery_timeout(self) -> None:
        current_time = [100.0]
        circuits = InMemoryCircuitBreaker(
            failure_threshold=1,
            recovery_timeout_seconds=10,
            now=lambda: current_time[0],
        )
        circuits.record_failure("openai", "model", "upstream_timeout")

        self.assertTrue(circuits.is_open("openai", "model"))
        current_time[0] = 111.0
        self.assertFalse(circuits.is_open("openai", "model"))

    def test_repeated_failures_open_circuit_and_skip_provider_on_later_request(self) -> None:
        app = create_app(failed_response_providers={"ollama"})
        client = TestClient(app)
        request_body = {
            "model": "mixapi/balanced-chat",
            "input": "Circuit",
            "routing": {"objective": "lowest-cost"},
        }

        first = client.post("/v1/responses", headers=AUTH_HEADERS, json=request_body)
        second = client.post("/v1/responses", headers=AUTH_HEADERS, json=request_body)
        third = client.post("/v1/responses", headers=AUTH_HEADERS, json=request_body)
        fourth = client.post("/v1/responses", headers=AUTH_HEADERS, json=request_body)

        self.assertEqual(first.json()["route"]["attempts"], 2)
        self.assertEqual(second.json()["route"]["attempts"], 2)
        self.assertEqual(third.json()["route"]["attempts"], 2)
        self.assertEqual(fourth.status_code, 200)
        self.assertEqual(fourth.json()["route"]["attempts"], 1)
        self.assertEqual(fourth.json()["provider"], "openai")
        self.assertIn(
            "provider_circuit_open",
            {item["reason"] for item in fourth.json()["route"]["rejected_candidates"]},
        )

    def test_all_open_circuits_return_unavailable_without_new_attempts(self) -> None:
        previous_threshold = os.environ.get("MIXAPI_CIRCUIT_FAILURE_THRESHOLD")
        os.environ["MIXAPI_CIRCUIT_FAILURE_THRESHOLD"] = "1"
        try:
            app = create_app(
                failed_response_providers={"ollama", "openai", "gemini", "anthropic"}
            )
            client = TestClient(app)
            request_body = {
                "model": "mixapi/balanced-chat",
                "input": "All circuits",
                "routing": {"objective": "lowest-cost"},
            }
            client.post(
                "/v1/responses",
                headers={**AUTH_HEADERS, "X-Request-ID": "req_open_all"},
                json=request_body,
            )
            response = client.post(
                "/v1/responses",
                headers={**AUTH_HEADERS, "X-Request-ID": "req_skip_all"},
                json=request_body,
            )
            decision = client.get(
                "/v1/route-decisions/req_skip_all",
                headers=AUTH_HEADERS,
            )
        finally:
            if previous_threshold is None:
                os.environ.pop("MIXAPI_CIRCUIT_FAILURE_THRESHOLD", None)
            else:
                os.environ["MIXAPI_CIRCUIT_FAILURE_THRESHOLD"] = previous_threshold

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "all_provider_circuits_open")
        self.assertEqual(decision.status_code, 200)
        self.assertEqual(decision.json()["attempts"], [])
        self.assertEqual(
            {item["reason"] for item in decision.json()["rejected_candidates"]},
            {"provider_circuit_open"},
        )


if __name__ == "__main__":
    unittest.main()
