import csv
import io
import os
import unittest
from decimal import Decimal

from fastapi.testclient import TestClient

from mixapi.app import create_app


DEV_HEADERS = {"Authorization": "Bearer dev-key"}


class UsageEndpointTest(unittest.TestCase):
    def test_usage_endpoint_lists_events_and_aggregate_totals(self) -> None:
        app = create_app()
        client = TestClient(app)

        client.post(
            "/v1/responses",
            headers=DEV_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Usage data",
                "native": {"provider": "openai", "provider_options": {}},
            },
        )
        client.post(
            "/v1/embeddings",
            headers=DEV_HEADERS,
            json={"model": "mixapi/embedding-small", "input": "Embedding usage"},
        )

        response = client.get("/v1/usage", headers=DEV_HEADERS)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["object"], "list")
        self.assertEqual(len(body["data"]), 2)
        self.assertEqual(body["summary"]["request_count"], 2)
        self.assertEqual(
            body["summary"]["input_tokens"],
            sum(event["input_tokens"] for event in body["data"]),
        )
        self.assertEqual(
            body["summary"]["output_tokens"],
            sum(event["output_tokens"] for event in body["data"]),
        )
        self.assertEqual(
            Decimal(body["summary"]["cost_usd"]),
            sum(Decimal(event["cost_usd"]) for event in body["data"]),
        )

    def test_usage_endpoint_is_tenant_scoped(self) -> None:
        previous_keys = os.environ.get("MIXAPI_API_KEYS")
        os.environ["MIXAPI_API_KEYS"] = "other-usage-key"
        try:
            app = create_app()
            client = TestClient(app)
            client.post(
                "/v1/responses",
                headers=DEV_HEADERS,
                json={"model": "mixapi/balanced-chat", "input": "Dev usage"},
            )
            client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer other-usage-key"},
                json={"model": "mixapi/balanced-chat", "input": "Other usage"},
            )

            dev_usage = client.get("/v1/usage", headers=DEV_HEADERS)
            other_usage = client.get(
                "/v1/usage",
                headers={"Authorization": "Bearer other-usage-key"},
            )
        finally:
            if previous_keys is None:
                os.environ.pop("MIXAPI_API_KEYS", None)
            else:
                os.environ["MIXAPI_API_KEYS"] = previous_keys

        self.assertEqual(dev_usage.status_code, 200)
        self.assertEqual(other_usage.status_code, 200)
        self.assertEqual(len(dev_usage.json()["data"]), 1)
        self.assertEqual(len(other_usage.json()["data"]), 1)
        self.assertEqual(dev_usage.json()["data"][0]["tenant_id"], "tenant_dev")
        self.assertEqual(other_usage.json()["data"][0]["tenant_id"], "tenant_env")

    def test_usage_export_returns_tenant_scoped_csv_download(self) -> None:
        previous_keys = os.environ.get("MIXAPI_API_KEYS")
        os.environ["MIXAPI_API_KEYS"] = "other-export-key"
        try:
            app = create_app()
            client = TestClient(app)
            client.post(
                "/v1/responses",
                headers=DEV_HEADERS,
                json={"model": "mixapi/balanced-chat", "input": "CSV usage"},
            )
            client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer other-export-key"},
                json={"model": "mixapi/balanced-chat", "input": "Other CSV usage"},
            )

            response = client.get("/v1/usage/export?format=csv", headers=DEV_HEADERS)
        finally:
            if previous_keys is None:
                os.environ.pop("MIXAPI_API_KEYS", None)
            else:
                os.environ["MIXAPI_API_KEYS"] = previous_keys

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/csv"))
        self.assertEqual(
            response.headers["content-disposition"],
            'attachment; filename="mixapi-usage.csv"',
        )
        rows = list(csv.DictReader(io.StringIO(response.text)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tenant_id"], "tenant_dev")
        self.assertEqual(rows[0]["endpoint"], "responses")
        self.assertEqual(rows[0]["logical_model"], "mixapi/balanced-chat")

    def test_usage_export_rejects_unsupported_format(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.get("/v1/usage/export?format=jsonl", headers=DEV_HEADERS)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["type"], "validation_error")
        self.assertEqual(response.json()["error"]["code"], "unsupported_usage_export_format")


if __name__ == "__main__":
    unittest.main()
