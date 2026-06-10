import json
import os
import unittest
from datetime import datetime, timedelta, timezone

import psycopg
import redis
from fastapi.testclient import TestClient

from mixapi.app import create_app


ADMIN_HEADERS = {"Authorization": "Bearer admin-secret"}


class AdminApiTest(unittest.TestCase):
    def setUp(self) -> None:
        with psycopg.connect(os.environ["MIXAPI_DATABASE_URL"]) as connection:
            connection.execute(
                "TRUNCATE api_keys, tenant_policies, audit_events RESTART IDENTITY CASCADE"
            )
        redis_client = redis.Redis.from_url(os.environ["MIXAPI_REDIS_URL"])
        redis_client.flushdb()
        redis_client.close()
        self.app = create_app(admin_api_key="admin-secret")
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

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

    def test_managed_api_key_authenticates_for_public_endpoints(self) -> None:
        created = self._create_key().json()

        response = self.client.get(
            "/v1/models",
            headers=self._service_headers(created["secret"]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["object"], "list")

    def test_revoked_managed_api_key_is_rejected_immediately(self) -> None:
        created = self._create_key().json()
        self.client.delete(
            f"/admin/v1/api-keys/{created['id']}",
            headers=ADMIN_HEADERS,
        )

        response = self.client.get(
            "/v1/models",
            headers=self._service_headers(created["secret"]),
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_api_key")

    def test_expired_managed_api_key_is_rejected_immediately(self) -> None:
        created = self._create_key().json()
        self.app.state.control_plane.update_api_key(
            created["id"],
            {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
            actor_id="admin",
        )

        response = self.client.get(
            "/v1/models",
            headers=self._service_headers(created["secret"]),
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_api_key")

    def test_managed_api_key_scope_is_enforced(self) -> None:
        created = self._create_key(scopes=["models:read"]).json()

        response = self.client.post(
            "/v1/responses",
            headers=self._service_headers(created["secret"]),
            json={"model": "mixapi/balanced-chat", "input": "Hello"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["type"], "permission_denied")
        self.assertEqual(response.json()["error"]["code"], "missing_scope")

    def test_key_and_tenant_model_allowlists_intersect_for_listing_and_dispatch(self) -> None:
        created = self._create_key(
            scopes=["models:read", "responses:create", "embeddings:create"],
            model_allowlist=["mixapi/balanced-chat", "mixapi/embedding-small"],
        ).json()
        self.client.put(
            "/admin/v1/tenants/tenant_acme/model-allowlist",
            headers=ADMIN_HEADERS,
            json={"models": ["mixapi/balanced-chat"]},
        )
        service_headers = self._service_headers(created["secret"])

        listed = self.client.get("/v1/models", headers=service_headers)
        denied = self.client.post(
            "/v1/embeddings",
            headers=service_headers,
            json={"model": "mixapi/embedding-small", "input": "Hello"},
        )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(
            [model["id"] for model in listed.json()["data"]],
            ["mixapi/balanced-chat"],
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "model_not_allowed")

    def test_disjoint_key_and_tenant_allowlists_deny_all_models(self) -> None:
        created = self._create_key(
            model_allowlist=["mixapi/balanced-chat"],
        ).json()
        self.client.put(
            "/admin/v1/tenants/tenant_acme/model-allowlist",
            headers=ADMIN_HEADERS,
            json={"models": ["mixapi/embedding-small"]},
        )
        service_headers = self._service_headers(created["secret"])

        listed = self.client.get("/v1/models", headers=service_headers)
        denied = self.client.post(
            "/v1/responses",
            headers=service_headers,
            json={"model": "mixapi/balanced-chat", "input": "Hello"},
        )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["data"], [])
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "model_not_allowed")

    def test_tenant_routing_default_applies_only_without_request_override(self) -> None:
        created = self._create_key(scopes=["responses:create"]).json()
        self.client.put(
            "/admin/v1/tenants/tenant_acme/routing-policy",
            headers=ADMIN_HEADERS,
            json={"objective": "highest-reliability"},
        )
        service_headers = self._service_headers(created["secret"])
        request_body = {
            "model": "mixapi/balanced-chat",
            "input": "Route this",
            "max_output_tokens": 4,
        }

        defaulted = self.client.post(
            "/v1/responses",
            headers=service_headers,
            json=request_body,
        )
        overridden = self.client.post(
            "/v1/responses",
            headers=service_headers,
            json={**request_body, "routing": {"objective": "lowest-cost"}},
        )

        self.assertEqual(defaulted.status_code, 200)
        self.assertEqual(defaulted.json()["provider"], "openai")
        self.assertEqual(overridden.status_code, 200)
        self.assertEqual(overridden.json()["provider"], "ollama")

    def test_managed_api_key_budgets_are_isolated(self) -> None:
        low_budget = self._create_key(
            project_id="project_low",
            scopes=["responses:create"],
            budget_limit_usd="0.00001000",
        ).json()
        high_budget = self._create_key(
            project_id="project_high",
            scopes=["responses:create"],
            budget_limit_usd="0.00010000",
        ).json()
        request_body = {
            "model": "mixapi/balanced-chat",
            "input": "Budget",
            "max_output_tokens": 4,
            "native": {"provider": "openai"},
        }

        first_low = self.client.post(
            "/v1/responses",
            headers=self._service_headers(low_budget["secret"]),
            json=request_body,
        )
        second_low = self.client.post(
            "/v1/responses",
            headers=self._service_headers(low_budget["secret"]),
            json=request_body,
        )
        first_high = self.client.post(
            "/v1/responses",
            headers=self._service_headers(high_budget["secret"]),
            json=request_body,
        )

        self.assertEqual(first_low.status_code, 200)
        self.assertEqual(second_low.status_code, 402)
        self.assertEqual(second_low.json()["error"]["code"], "api_key_budget_exceeded")
        self.assertEqual(first_high.status_code, 200)

    def test_managed_configuration_survives_application_restart(self) -> None:
        created = self._create_key(scopes=["models:read"]).json()

        restarted_app = create_app(admin_api_key="admin-secret")
        with TestClient(restarted_app) as restarted_client:
            response = restarted_client.get(
                "/v1/models",
                headers=self._service_headers(created["secret"]),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["object"], "list")

    def _create_key(self, client=None, **overrides):
        target_client = client or self.client
        payload = {
            "tenant_id": "tenant_acme",
            "project_id": "project_chat",
            "name": "chat service",
            "scopes": ["models:read", "responses:create"],
            "model_allowlist": ["mixapi/balanced-chat"],
            "budget_limit_usd": "1.25",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(days=1)
            ).isoformat(),
        }
        payload.update(overrides)
        return target_client.post(
            "/admin/v1/api-keys",
            headers=ADMIN_HEADERS,
            json=payload,
        )

    @staticmethod
    def _service_headers(secret: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {secret}"}


if __name__ == "__main__":
    unittest.main()
