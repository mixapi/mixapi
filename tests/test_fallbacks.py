import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mixapi.adapters import DeterministicProviderAdapter, ProviderDispatchError
from tests.app_factory import create_app


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


class FallbacksTest(unittest.TestCase):
    def test_response_falls_back_to_next_eligible_provider(self) -> None:
        app = create_app(failed_response_providers={"ollama"})
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Fallback"}]}],
                "routing": {"objective": "lowest-cost"},
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "openai")
        self.assertEqual(body["route"]["attempts"], 2)
        self.assertTrue(body["route"]["fallback_used"])
        self.assertEqual(body["route"]["failed_attempts"][0]["provider"], "ollama")
        self.assertEqual(body["route"]["selected_provider_model"], "gpt-4.1-mini")
        self.assertEqual(app.state.usage_ledger.events()[0].provider, "openai")

    def test_response_returns_provider_unavailable_when_all_candidates_fail(self) -> None:
        app = create_app(failed_response_providers={"ollama", "openai", "gemini", "anthropic"})
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "All fail"}]}],
                "routing": {"objective": "lowest-cost"},
            },
        )

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["error"]["type"], "provider_unavailable")
        self.assertEqual(body["error"]["code"], "all_candidates_failed")
        self.assertEqual(len(app.state.usage_ledger.events()), 0)

    def test_response_returns_provider_rate_limited_when_all_attempts_are_429(self) -> None:
        app = create_app()
        client = TestClient(app)

        def raise_rate_limit(_adapter, _request_body, candidate):
            raise ProviderDispatchError(
                provider=candidate.provider,
                provider_model_id=candidate.provider_model_id,
                reason="upstream_http_429",
            )

        with patch.object(
            DeterministicProviderAdapter,
            "dispatch_response",
            autospec=True,
            side_effect=raise_rate_limit,
        ):
            response = client.post(
                "/v1/responses",
                headers=AUTH_HEADERS,
                json={
                    "model": "mixapi/balanced-chat",
                    "input": "Rate limited",
                    "native": {"provider": "openai"},
                },
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["type"], "provider_rate_limited")
        self.assertEqual(response.json()["error"]["code"], "all_candidates_rate_limited")

    def test_embedding_returns_upstream_timeout_when_all_attempts_time_out(self) -> None:
        app = create_app()
        client = TestClient(app)

        def raise_timeout(_adapter, _request_body, candidate):
            raise ProviderDispatchError(
                provider=candidate.provider,
                provider_model_id=candidate.provider_model_id,
                reason="upstream_timeout",
            )

        with patch.object(
            DeterministicProviderAdapter,
            "dispatch_embedding",
            autospec=True,
            side_effect=raise_timeout,
        ):
            response = client.post(
                "/v1/embeddings",
                headers=AUTH_HEADERS,
                json={
                    "model": "mixapi/embedding-small",
                    "input": "Timeout",
                    "native": {"provider": "openai"},
                },
            )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error"]["type"], "upstream_timeout")
        self.assertEqual(response.json()["error"]["code"], "all_candidates_timed_out")


if __name__ == "__main__":
    unittest.main()
