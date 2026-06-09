import json
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from mixapi.app import create_app


ADMIN_HEADERS = {"Authorization": "Bearer admin-secret"}


class AdminApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(admin_api_key="admin-secret")
        self.client = TestClient(self.app)

    def test_admin_endpoints_require_separate_operator_credential(self) -> None:
        missing = self.client.get("/admin/v1/api-keys")
        wrong = self.client.get(
            "/admin/v1/api-keys",
            headers={"Authorization": "Bearer wrong"},
        )
        service_key = self.client.get(
            "/admin/v1/api-keys",
            headers={"Authorization": "Bearer dev-key"},
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json()["error"]["code"], "missing_admin_key")
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.json()["error"]["code"], "invalid_admin_key")
        self.assertEqual(service_key.status_code, 401)
        self.assertEqual(service_key.json()["error"]["code"], "invalid_admin_key")

    def test_api_key_lifecycle_returns_secret_once_and_lists_redacted_records(self) -> None:
        created = self._create_key()

        self.assertEqual(created.status_code, 201)
        created_body = created.json()
        self.assertTrue(created_body["secret"].startswith("mxapi_"))
        self.assertEqual(created_body["tenant_id"], "tenant_acme")
        self.assertNotIn("key_hash", created_body)

        listed = self.client.get("/admin/v1/api-keys", headers=ADMIN_HEADERS)
        listed_body = listed.json()["data"]
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed_body), 1)
        self.assertNotIn("secret", listed_body[0])
        self.assertNotIn("key_hash", listed_body[0])

        updated = self.client.patch(
            f"/admin/v1/api-keys/{created_body['id']}",
            headers=ADMIN_HEADERS,
            json={"name": "renamed", "budget_limit_usd": "2.50"},
        )
        revoked = self.client.delete(
            f"/admin/v1/api-keys/{created_body['id']}",
            headers=ADMIN_HEADERS,
        )

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["name"], "renamed")
        self.assertEqual(updated.json()["budget_limit_usd"], "2.50")
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.json()["status"], "revoked")

    def test_tenant_allowlist_and_routing_policy_are_managed_separately(self) -> None:
        allowlist = self.client.put(
            "/admin/v1/tenants/tenant_acme/model-allowlist",
            headers=ADMIN_HEADERS,
            json={"models": ["mixapi/balanced-chat"]},
        )
        routing = self.client.put(
            "/admin/v1/tenants/tenant_acme/routing-policy",
            headers=ADMIN_HEADERS,
            json={"objective": "lowest-cost"},
        )

        self.assertEqual(allowlist.status_code, 200)
        self.assertEqual(allowlist.json()["model_allowlist"], ["mixapi/balanced-chat"])
        self.assertIsNone(allowlist.json()["routing_objective"])
        self.assertEqual(routing.status_code, 200)
        self.assertEqual(routing.json()["model_allowlist"], ["mixapi/balanced-chat"])
        self.assertEqual(routing.json()["routing_objective"], "lowest-cost")

    def test_audit_events_are_redacted_and_tenant_filterable(self) -> None:
        created = self._create_key().json()
        self.client.put(
            "/admin/v1/tenants/tenant_other/model-allowlist",
            headers=ADMIN_HEADERS,
            json={"models": []},
        )

        response = self.client.get(
            "/admin/v1/audit-events",
            headers=ADMIN_HEADERS,
            params={"tenant_id": "tenant_acme"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(len(data), 1)
        serialized = json.dumps(data)
        self.assertNotIn(created["secret"], serialized)
        self.assertNotIn("key_hash", serialized)

    def test_invalid_admin_payloads_return_normalized_validation_errors(self) -> None:
        cases = [
            (
                "/admin/v1/api-keys",
                {
                    "tenant_id": "",
                    "project_id": "project_chat",
                    "scopes": ["models:read"],
                },
                "invalid_tenant_id",
            ),
            (
                "/admin/v1/api-keys",
                {
                    "tenant_id": "tenant_acme",
                    "project_id": "project_chat",
                    "scopes": ["unknown:scope"],
                },
                "invalid_scopes",
            ),
            (
                "/admin/v1/api-keys",
                {
                    "tenant_id": "tenant_acme",
                    "project_id": "project_chat",
                    "model_allowlist": ["mixapi/not-real"],
                },
                "invalid_model_allowlist",
            ),
            (
                "/admin/v1/api-keys",
                {
                    "tenant_id": "tenant_acme",
                    "project_id": "project_chat",
                    "budget_limit_usd": "-1",
                },
                "invalid_budget_limit_usd",
            ),
            (
                "/admin/v1/api-keys",
                {
                    "tenant_id": "tenant_acme",
                    "project_id": "project_chat",
                    "expires_at": "yesterday",
                },
                "invalid_expires_at",
            ),
        ]

        for path, body, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                response = self.client.post(path, headers=ADMIN_HEADERS, json=body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], expected_code)

        routing = self.client.put(
            "/admin/v1/tenants/tenant_acme/routing-policy",
            headers=ADMIN_HEADERS,
            json={"objective": "random"},
        )
        self.assertEqual(routing.status_code, 400)
        self.assertEqual(routing.json()["error"]["code"], "invalid_routing_objective")

    def test_missing_api_key_update_returns_not_found(self) -> None:
        response = self.client.patch(
            "/admin/v1/api-keys/key_missing",
            headers=ADMIN_HEADERS,
            json={"name": "missing"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "api_key_not_found")

    def _create_key(self):
        return self.client.post(
            "/admin/v1/api-keys",
            headers=ADMIN_HEADERS,
            json={
                "tenant_id": "tenant_acme",
                "project_id": "project_chat",
                "name": "chat service",
                "scopes": ["models:read", "responses:create"],
                "model_allowlist": ["mixapi/balanced-chat"],
                "budget_limit_usd": "1.25",
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(days=1)
                ).isoformat(),
            },
        )


if __name__ == "__main__":
    unittest.main()
