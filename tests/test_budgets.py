import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mixapi.adapters import DeterministicProviderAdapter
from mixapi.app import create_app


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


class BudgetTest(unittest.TestCase):
    def test_request_cost_limit_rejects_before_provider_usage_is_recorded(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Budget",
                "max_output_tokens": 4,
                "routing": {"max_cost_usd": "0.000006"},
                "native": {"provider": "openai"},
            },
        )

        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["error"]["type"], "budget_exceeded")
        self.assertEqual(response.json()["error"]["code"], "request_budget_exceeded")
        self.assertEqual(app.state.usage_ledger.events(), [])

    def test_api_key_budget_rejects_request_when_actual_plus_reservation_exceeds_limit(self) -> None:
        previous_limit = os.environ.get("MIXAPI_BUDGET_LIMIT_USD")
        os.environ["MIXAPI_BUDGET_LIMIT_USD"] = "0.00001000"
        try:
            app = create_app()
            client = TestClient(app)
            request_body = {
                "model": "mixapi/balanced-chat",
                "input": "Budget",
                "max_output_tokens": 4,
                "native": {"provider": "openai"},
            }

            first = client.post("/v1/responses", headers=AUTH_HEADERS, json=request_body)
            second = client.post("/v1/responses", headers=AUTH_HEADERS, json=request_body)
        finally:
            if previous_limit is None:
                os.environ.pop("MIXAPI_BUDGET_LIMIT_USD", None)
            else:
                os.environ["MIXAPI_BUDGET_LIMIT_USD"] = previous_limit

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 402)
        self.assertEqual(second.json()["error"]["type"], "budget_exceeded")
        self.assertEqual(second.json()["error"]["code"], "api_key_budget_exceeded")
        self.assertEqual(len(app.state.usage_ledger.events()), 1)

    def test_request_cost_limit_must_be_a_positive_decimal(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Budget",
                "routing": {"max_cost_usd": -1},
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_max_cost_usd")

    def test_structured_request_cost_limit_reserves_corrective_retry(self) -> None:
        client = TestClient(create_app())

        with patch.object(
            DeterministicProviderAdapter,
            "dispatch_response",
            autospec=True,
        ) as dispatch:
            response = client.post(
                "/v1/responses",
                headers=AUTH_HEADERS,
                json={
                    "model": "mixapi/balanced-chat",
                    "input": "Return JSON",
                    "max_output_tokens": 8,
                    "routing": {"max_cost_usd": "0.00002000"},
                    "native": {"provider": "openai"},
                    "response": {
                        "format": {
                            "type": "json_schema",
                            "json_schema": {"type": "object"},
                        }
                    },
                },
            )

        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["error"]["code"], "request_budget_exceeded")
        dispatch.assert_not_called()

    def test_api_key_budget_reserves_structured_corrective_retry(self) -> None:
        client = TestClient(create_app(budget_limit_usd="0.00002000"))

        with patch.object(
            DeterministicProviderAdapter,
            "dispatch_response",
            autospec=True,
        ) as dispatch:
            response = client.post(
                "/v1/responses",
                headers=AUTH_HEADERS,
                json={
                    "model": "mixapi/balanced-chat",
                    "input": "Return JSON",
                    "max_output_tokens": 8,
                    "native": {"provider": "openai"},
                    "response": {
                        "format": {
                            "type": "json_schema",
                            "json_schema": {"type": "object"},
                        }
                    },
                },
            )

        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["error"]["code"], "api_key_budget_exceeded")
        dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
