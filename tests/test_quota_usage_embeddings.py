import unittest

from fastapi.testclient import TestClient

from mixapi.app import create_app


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


class QuotaUsageEmbeddingsTest(unittest.TestCase):
    def test_response_records_usage_event(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Track usage"}]}],
            },
        )

        self.assertEqual(response.status_code, 200)
        events = app.state.usage_ledger.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].endpoint, "responses")
        self.assertEqual(events[0].provider, response.json()["provider"])
        self.assertGreater(events[0].input_tokens, 0)

    def test_request_quota_denies_second_request(self) -> None:
        app = create_app(request_quota_limit=1)
        client = TestClient(app)
        payload = {
            "model": "mixapi/balanced-chat",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "Quota"}]}],
        }

        first = client.post("/v1/responses", headers=AUTH_HEADERS, json=payload)
        second = client.post("/v1/responses", headers=AUTH_HEADERS, json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["error"]["type"], "quota_exceeded")
        self.assertEqual(second.json()["error"]["code"], "request_quota_exceeded")
        self.assertEqual(len(app.state.usage_ledger.events()), 1)

    def test_token_quota_rejects_estimated_usage_before_dispatch(self) -> None:
        app = create_app(token_quota_limit=4)
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Quota",
                "max_output_tokens": 4,
                "native": {"provider": "openai"},
            },
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["type"], "quota_exceeded")
        self.assertEqual(response.json()["error"]["code"], "token_quota_exceeded")
        self.assertEqual(app.state.usage_ledger.events(), [])

    def test_token_quota_reconciles_estimate_to_actual_usage(self) -> None:
        app = create_app(token_quota_limit=8)
        client = TestClient(app)
        payload = {
            "model": "mixapi/balanced-chat",
            "input": "Quota",
            "max_output_tokens": 4,
            "native": {"provider": "openai"},
        }

        first = client.post("/v1/responses", headers=AUTH_HEADERS, json=payload)
        second = client.post("/v1/responses", headers=AUTH_HEADERS, json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(app.state.usage_ledger.events()), 2)

    def test_provider_failure_releases_token_reservation(self) -> None:
        app = create_app(
            token_quota_limit=5,
            failed_response_providers={"openai"},
        )
        client = TestClient(app)
        base_payload = {
            "model": "mixapi/balanced-chat",
            "input": "Quota",
            "max_output_tokens": 4,
        }

        failed = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={**base_payload, "native": {"provider": "openai"}},
        )
        recovered = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={**base_payload, "native": {"provider": "ollama"}},
        )

        self.assertEqual(failed.status_code, 503)
        self.assertEqual(recovered.status_code, 200)

    def test_embeddings_endpoint_returns_vector_and_records_usage(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.post(
            "/v1/embeddings",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/embedding-small",
                "input": "hello world",
                "routing": {"objective": "lowest-cost"},
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["object"], "list")
        self.assertEqual(body["provider"], "ollama")
        self.assertEqual(len(body["data"]), 1)
        self.assertEqual(len(body["data"][0]["embedding"]), 8)
        self.assertEqual(body["usage"]["input_tokens"], 2)
        self.assertEqual(app.state.usage_ledger.events()[0].endpoint, "embeddings")

    def test_embeddings_consume_token_quota(self) -> None:
        app = create_app(token_quota_limit=3)
        client = TestClient(app)
        payload = {
            "model": "mixapi/embedding-small",
            "input": "hello world",
        }

        first = client.post("/v1/embeddings", headers=AUTH_HEADERS, json=payload)
        second = client.post("/v1/embeddings", headers=AUTH_HEADERS, json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["error"]["code"], "token_quota_exceeded")


if __name__ == "__main__":
    unittest.main()
