import json
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from mixapi.app import create_app


ROOT = Path(__file__).resolve().parents[1]


class OpenAPIContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(admin_api_key="admin-secret")
        self.contract = self.app.openapi()

    def test_contract_has_stable_operations_and_security_schemes(self) -> None:
        self.assertEqual(self.contract["openapi"], "3.1.0")
        self.assertEqual(
            self.contract["components"]["securitySchemes"],
            {
                "ServiceBearerAuth": {"type": "http", "scheme": "bearer"},
                "AdminBearerAuth": {"type": "http", "scheme": "bearer"},
            },
        )
        expected_operations = {
            ("/v1/models", "get"): "listModels",
            ("/v1/responses", "post"): "createResponse",
            ("/v1/embeddings", "post"): "createEmbedding",
            ("/v1/route-decisions/{request_id}", "get"): "getRouteDecision",
            ("/v1/usage", "get"): "getUsage",
            ("/v1/usage/export", "get"): "exportUsage",
            ("/admin/v1/api-keys", "post"): "createApiKey",
            ("/admin/v1/api-keys", "get"): "listApiKeys",
            ("/admin/v1/api-keys/{api_key_id}", "patch"): "updateApiKey",
            ("/admin/v1/api-keys/{api_key_id}", "delete"): "revokeApiKey",
            (
                "/admin/v1/tenants/{tenant_id}/model-allowlist",
                "put",
            ): "setTenantModelAllowlist",
            (
                "/admin/v1/tenants/{tenant_id}/routing-policy",
                "put",
            ): "setTenantRoutingPolicy",
            ("/admin/v1/audit-events", "get"): "listAuditEvents",
        }

        for (path, method), operation_id in expected_operations.items():
            with self.subTest(path=path, method=method):
                operation = self.contract["paths"][path][method]
                self.assertEqual(operation["operationId"], operation_id)
                expected_security = (
                    [{"AdminBearerAuth": []}]
                    if path.startswith("/admin/")
                    else [{"ServiceBearerAuth": []}]
                )
                self.assertEqual(operation["security"], expected_security)

    def test_json_operations_reference_explicit_contract_models(self) -> None:
        response_operation = self.contract["paths"]["/v1/responses"]["post"]
        self.assertEqual(
            response_operation["requestBody"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/ResponseCreateRequest"},
        )
        self.assertEqual(
            response_operation["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/ResponseObject"},
        )
        self.assertEqual(
            response_operation["responses"]["400"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/ErrorEnvelope"},
        )

        embedding_operation = self.contract["paths"]["/v1/embeddings"]["post"]
        self.assertEqual(
            embedding_operation["requestBody"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/EmbeddingCreateRequest"},
        )
        self.assertEqual(
            embedding_operation["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/EmbeddingList"},
        )

        schemas = self.contract["components"]["schemas"]
        for schema_name in (
            "ModelList",
            "ResponseCreateRequest",
            "ResponseObject",
            "EmbeddingCreateRequest",
            "EmbeddingList",
            "RouteDecision",
            "UsageList",
            "ApiKeyCreateRequest",
            "ApiKeyCreated",
            "ApiKeyList",
            "ApiKeyUpdateRequest",
            "TenantModelAllowlistRequest",
            "TenantRoutingPolicyRequest",
            "TenantPolicy",
            "AuditEventList",
            "ErrorEnvelope",
        ):
            with self.subTest(schema_name=schema_name):
                self.assertIn(schema_name, schemas)

    def test_streaming_and_export_media_types_are_documented(self) -> None:
        response_operation = self.contract["paths"]["/v1/responses"]["post"]
        stream_schema = response_operation["responses"]["200"]["content"][
            "text/event-stream"
        ]["schema"]
        self.assertEqual(
            stream_schema["oneOf"],
            [
                {"$ref": "#/components/schemas/ResponseCreatedEvent"},
                {"$ref": "#/components/schemas/ResponseOutputTextDeltaEvent"},
                {"$ref": "#/components/schemas/ResponseCompletedEvent"},
                {"$ref": "#/components/schemas/ResponseErrorEvent"},
            ],
        )

        export_operation = self.contract["paths"]["/v1/usage/export"]["get"]
        content = export_operation["responses"]["200"]["content"]
        self.assertEqual(content["text/csv"]["schema"], {"type": "string"})
        self.assertEqual(
            content["application/x-ndjson"]["schema"],
            {"type": "string"},
        )

    def test_openapi_endpoint_serves_the_application_contract(self) -> None:
        response = TestClient(self.app).get("/openapi.json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.contract)

    def test_checked_in_contract_matches_application_contract(self) -> None:
        expected = json.dumps(self.contract, indent=2, sort_keys=True) + "\n"

        self.assertEqual((ROOT / "openapi" / "openapi.json").read_text(), expected)


if __name__ == "__main__":
    unittest.main()
