import unittest
import os

import psycopg
from fastapi.testclient import TestClient

from mixapi.app import create_app


AUTH_HEADERS = {
    "Authorization": "Bearer dev-key",
    "X-Request-ID": "req_route_1",
}


class RouteDecisionsTest(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(os.environ["MIXAPI_DATABASE_URL"]) as connection:
            connection.execute(
                """
                TRUNCATE usage_events, route_decisions, budget_spend,
                         usage_write_intents, budget_reconciliation_outbox
                RESTART IDENTITY CASCADE
                """
            )

    def _client(self, app=None) -> TestClient:
        return self.enterContext(TestClient(app or create_app()))

    def test_route_decision_is_persisted_and_readable(self) -> None:
        app = create_app(failed_response_providers={"ollama"})
        client = self._client(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Trace route"}]}],
                "routing": {"objective": "lowest-cost"},
            },
        )
        route_response = client.get("/v1/route-decisions/req_route_1", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(route_response.status_code, 200)
        route = route_response.json()
        self.assertEqual(route["request_id"], "req_route_1")
        self.assertEqual(route["status"], "succeeded")
        self.assertEqual(route["selected_provider"], "openai")
        self.assertEqual(route["selected_provider_model"], "gpt-4.1-mini")
        self.assertEqual(route["attempts"][0]["provider"], "ollama")
        self.assertEqual(route["attempts"][0]["status"], "failed")
        self.assertEqual(route["attempts"][1]["provider"], "openai")
        self.assertEqual(route["attempts"][1]["status"], "succeeded")
        self.assertTrue(route["fallback_used"])

    def test_missing_route_decision_returns_normalized_not_found(self) -> None:
        client = self._client()

        response = client.get("/v1/route-decisions/req_missing", headers=AUTH_HEADERS)

        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["error"]["type"], "not_found")
        self.assertEqual(body["error"]["code"], "route_decision_not_found")

    def test_route_decision_lookup_is_tenant_scoped(self) -> None:
        previous_keys = os.environ.get("MIXAPI_API_KEYS")
        os.environ["MIXAPI_API_KEYS"] = "other-tenant-key"
        try:
            app = create_app()
            client = self._client(app)
            create_response = client.post(
                "/v1/responses",
                headers=AUTH_HEADERS,
                json={
                    "model": "mixapi/balanced-chat",
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": "Private"}]}],
                },
            )
            read_response = client.get(
                "/v1/route-decisions/req_route_1",
                headers={"Authorization": "Bearer other-tenant-key"},
            )
        finally:
            if previous_keys is None:
                os.environ.pop("MIXAPI_API_KEYS", None)
            else:
                os.environ["MIXAPI_API_KEYS"] = previous_keys

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(read_response.status_code, 404)
        self.assertEqual(read_response.json()["error"]["code"], "route_decision_not_found")


if __name__ == "__main__":
    unittest.main()
