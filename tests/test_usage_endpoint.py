import csv
import io
import json
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal

import psycopg
from fastapi.testclient import TestClient

from mixapi.app import create_app
from mixapi.usage import UsageEvent


DEV_HEADERS = {"Authorization": "Bearer dev-key"}


class UsageEndpointTest(unittest.TestCase):
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

    def test_usage_events_include_utc_creation_time(self) -> None:
        app = create_app()
        client = self._client(app)

        client.post(
            "/v1/responses",
            headers=DEV_HEADERS,
            json={"model": "mixapi/balanced-chat", "input": "Timestamp usage"},
        )

        response = client.get("/v1/usage", headers=DEV_HEADERS)

        self.assertEqual(response.status_code, 200)
        created_at = response.json()["data"][0]["created_at"]
        parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        self.assertEqual(parsed_created_at.tzinfo, timezone.utc)

    def test_usage_endpoint_filters_by_inclusive_time_window(self) -> None:
        app = create_app()
        client = self._client(app)
        for request_id, created_at in (
            ("req_before", datetime(2026, 6, 9, 9, 59, tzinfo=timezone.utc)),
            ("req_start", datetime(2026, 6, 9, 10, 0, tzinfo=timezone.utc)),
            ("req_end", datetime(2026, 6, 9, 11, 0, tzinfo=timezone.utc)),
            ("req_after", datetime(2026, 6, 9, 11, 1, tzinfo=timezone.utc)),
        ):
            app.state.usage_ledger.record(_usage_event(request_id, created_at))

        response = client.get(
            "/v1/usage",
            headers=DEV_HEADERS,
            params={
                "start_time": "2026-06-09T10:00:00Z",
                "end_time": "2026-06-09T11:00:00Z",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [event["request_id"] for event in response.json()["data"]],
            ["req_start", "req_end"],
        )

    def test_usage_endpoint_paginates_without_duplicates(self) -> None:
        app = create_app()
        client = self._client(app)
        for index in range(3):
            app.state.usage_ledger.record(
                _usage_event(
                    f"req_page_{index}",
                    datetime(2026, 6, 9, 10, index, tzinfo=timezone.utc),
                )
            )

        first = client.get("/v1/usage", headers=DEV_HEADERS, params={"limit": 2})
        first_body = first.json()
        second = client.get(
            "/v1/usage",
            headers=DEV_HEADERS,
            params={"limit": 2, "cursor": first_body["next_cursor"]},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            [event["request_id"] for event in first_body["data"]],
            ["req_page_0", "req_page_1"],
        )
        self.assertTrue(first_body["has_more"])
        self.assertEqual(first_body["summary"]["request_count"], 2)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            [event["request_id"] for event in second.json()["data"]],
            ["req_page_2"],
        )
        self.assertFalse(second.json()["has_more"])
        self.assertIsNone(second.json()["next_cursor"])

    def test_usage_endpoint_rejects_invalid_limit(self) -> None:
        client = self._client()

        response = client.get("/v1/usage", headers=DEV_HEADERS, params={"limit": 0})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_usage_limit")

    def test_usage_endpoint_rejects_invalid_timestamp(self) -> None:
        client = self._client()

        response = client.get(
            "/v1/usage",
            headers=DEV_HEADERS,
            params={"start_time": "not-a-timestamp"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_usage_timestamp")

    def test_usage_endpoint_rejects_reversed_time_window(self) -> None:
        client = self._client()

        response = client.get(
            "/v1/usage",
            headers=DEV_HEADERS,
            params={
                "start_time": "2026-06-09T11:00:00Z",
                "end_time": "2026-06-09T10:00:00Z",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_usage_time_range")

    def test_usage_cursor_is_bound_to_time_window(self) -> None:
        app = create_app()
        client = self._client(app)
        app.state.usage_ledger.record(
            _usage_event("req_cursor", datetime(2026, 6, 9, 10, 0, tzinfo=timezone.utc))
        )
        app.state.usage_ledger.record(
            _usage_event("req_cursor_2", datetime(2026, 6, 9, 10, 1, tzinfo=timezone.utc))
        )
        first = client.get(
            "/v1/usage",
            headers=DEV_HEADERS,
            params={"limit": 1, "start_time": "2026-06-09T10:00:00Z"},
        )

        response = client.get(
            "/v1/usage",
            headers=DEV_HEADERS,
            params={
                "limit": 1,
                "start_time": "2026-06-09T09:00:00Z",
                "cursor": first.json()["next_cursor"],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_usage_cursor")

    def test_usage_endpoint_rejects_malformed_cursor(self) -> None:
        client = self._client()

        response = client.get(
            "/v1/usage",
            headers=DEV_HEADERS,
            params={"cursor": "____"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_usage_cursor")

    def test_usage_endpoint_lists_events_and_aggregate_totals(self) -> None:
        app = create_app()
        client = self._client(app)

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
            client = self._client(app)
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
            client = self._client(app)
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
        self.assertNotEqual(rows[0]["configuration_version"], "0")
        self.assertEqual(rows[0]["provider_connection_id"], "provider_openai")
        self.assertEqual(rows[0]["provider_protocol"], "openai-compatible")

    def test_usage_csv_export_honors_time_window(self) -> None:
        app = create_app()
        client = self._client(app)
        app.state.usage_ledger.record(
            _usage_event("req_csv_before", datetime(2026, 6, 9, 9, 59, tzinfo=timezone.utc))
        )
        app.state.usage_ledger.record(
            _usage_event("req_csv_inside", datetime(2026, 6, 9, 10, 0, tzinfo=timezone.utc))
        )

        response = client.get(
            "/v1/usage/export",
            headers=DEV_HEADERS,
            params={
                "format": "csv",
                "start_time": "2026-06-09T10:00:00Z",
                "end_time": "2026-06-09T10:00:00Z",
            },
        )

        self.assertEqual(response.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(response.text)))
        self.assertEqual([row["request_id"] for row in rows], ["req_csv_inside"])

    def test_usage_export_returns_filtered_tenant_scoped_jsonl(self) -> None:
        app = create_app()
        client = self._client(app)
        app.state.usage_ledger.record(
            _usage_event("req_jsonl_before", datetime(2026, 6, 9, 9, 59, tzinfo=timezone.utc))
        )
        app.state.usage_ledger.record(
            _usage_event("req_jsonl_dev", datetime(2026, 6, 9, 10, 0, tzinfo=timezone.utc))
        )
        app.state.usage_ledger.record(
            _usage_event(
                "req_jsonl_other",
                datetime(2026, 6, 9, 10, 0, tzinfo=timezone.utc),
                tenant_id="tenant_other",
            )
        )

        response = client.get(
            "/v1/usage/export",
            headers=DEV_HEADERS,
            params={
                "format": "jsonl",
                "start_time": "2026-06-09T10:00:00Z",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("application/x-ndjson"))
        self.assertEqual(
            response.headers["content-disposition"],
            'attachment; filename="mixapi-usage.jsonl"',
        )
        rows = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual([row["request_id"] for row in rows], ["req_jsonl_dev"])
        self.assertEqual(rows[0]["tenant_id"], "tenant_dev")

    def test_usage_export_rejects_unsupported_format(self) -> None:
        app = create_app()
        client = self._client(app)

        response = client.get("/v1/usage/export?format=xml", headers=DEV_HEADERS)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["type"], "validation_error")
        self.assertEqual(response.json()["error"]["code"], "unsupported_usage_export_format")


def _usage_event(
    request_id: str,
    created_at: datetime,
    *,
    tenant_id: str = "tenant_dev",
) -> UsageEvent:
    return UsageEvent(
        request_id=request_id,
        tenant_id=tenant_id,
        project_id="project_dev",
        api_key_id="key_dev",
        endpoint="responses",
        logical_model="mixapi/balanced-chat",
        provider="openai",
        provider_model="gpt-test",
        input_tokens=1,
        output_tokens=1,
        cost_usd=Decimal("0.000001"),
        created_at=created_at,
    )


if __name__ == "__main__":
    unittest.main()
