import json
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from mixapi.api_contract import CONTRACT_MODELS
from mixapi.app import create_app


ROOT = Path(__file__).resolve().parents[1]


class OpenAPIContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(admin_api_key="admin-secret")
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.contract = self.app.openapi()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)

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
            ("/admin/v1/providers", "post"): "createProviderConnection",
            ("/admin/v1/providers", "get"): "listProviderConnections",
            ("/admin/v1/providers/{provider_id}", "get"): "getProviderConnection",
            ("/admin/v1/providers/{provider_id}", "patch"): "updateProviderConnection",
            ("/admin/v1/providers/{provider_id}", "delete"): "deleteProviderConnection",
            ("/admin/v1/providers/{provider_id}/test", "post"): "testProviderConnection",
            ("/admin/v1/models", "post"): "createLogicalModel",
            ("/admin/v1/models", "get"): "listLogicalModels",
            ("/admin/v1/models/{model_id}", "get"): "getLogicalModel",
            ("/admin/v1/models/{model_id}", "patch"): "updateLogicalModel",
            ("/admin/v1/models/{model_id}", "delete"): "deleteLogicalModel",
            ("/admin/v1/candidates", "post"): "createModelCandidate",
            ("/admin/v1/candidates", "get"): "listModelCandidates",
            ("/admin/v1/candidates/{candidate_id}", "get"): "getModelCandidate",
            ("/admin/v1/candidates/{candidate_id}", "patch"): "updateModelCandidate",
            ("/admin/v1/candidates/{candidate_id}", "delete"): "deleteModelCandidate",
            (
                "/admin/v1/configuration/versions/{version}",
                "get",
            ): "getConfigurationVersion",
            ("/admin/v1/configuration/rebuild", "post"): "rebuildConfiguration",
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
        self.assertEqual(
            response_operation["responses"]["422"]["content"]["application/json"]["schema"],
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
            "ProviderCreateRequest",
            "ProviderUpdateRequest",
            "ProviderRecord",
            "ProviderList",
            "ProviderMutation",
            "ProviderTestResult",
            "LogicalModelCreateRequest",
            "LogicalModelUpdateRequest",
            "LogicalModelRecord",
            "LogicalModelList",
            "LogicalModelMutation",
            "ModelCandidateCreateRequest",
            "ModelCandidateUpdateRequest",
            "ModelCandidateRecord",
            "ModelCandidateList",
            "ModelCandidateMutation",
            "ConfigurationStatus",
            "ConfigurationRebuildResult",
            "ErrorEnvelope",
        ):
            with self.subTest(schema_name=schema_name):
                self.assertIn(schema_name, schemas)

    def test_dynamic_admin_mutations_and_credentials_are_explicit(self) -> None:
        for path, method in (
            ("/admin/v1/providers", "post"),
            ("/admin/v1/providers/{provider_id}", "patch"),
            ("/admin/v1/providers/{provider_id}", "delete"),
            ("/admin/v1/models", "post"),
            ("/admin/v1/models/{model_id}", "patch"),
            ("/admin/v1/models/{model_id}", "delete"),
            ("/admin/v1/candidates", "post"),
            ("/admin/v1/candidates/{candidate_id}", "patch"),
            ("/admin/v1/candidates/{candidate_id}", "delete"),
            ("/admin/v1/configuration/rebuild", "post"),
        ):
            with self.subTest(path=path, method=method):
                operation = self.contract["paths"][path][method]
                self.assertIn("202", operation["responses"])

        schemas = self.contract["components"]["schemas"]
        self.assertTrue(
            schemas["ProviderCreateRequest"]["properties"]["credential"]["writeOnly"]
        )
        self.assertTrue(
            schemas["ProviderUpdateRequest"]["properties"]["credential"]["writeOnly"]
        )
        self.assertNotIn("credential", schemas["ProviderRecord"]["properties"])

    def test_route_contracts_expose_snapshot_and_provider_connection(self) -> None:
        schemas = self.contract["components"]["schemas"]
        expected = {
            "RouteMetadata": {"provider_connection_id", "provider_protocol"},
            "RouteDecision": {
                "selected_provider_connection_id",
                "selected_provider_protocol",
            },
            "UsageEvent": {"provider_connection_id", "provider_protocol"},
        }
        for model_name, provider_fields in expected.items():
            with self.subTest(model_name=model_name):
                required = set(schemas[model_name]["required"])
                self.assertIn("configuration_version", required)
                self.assertTrue(provider_fields <= required)

    def test_response_format_documents_draft_2020_12_validation(self) -> None:
        schema = self.contract["components"]["schemas"]["ResponseFormat"]

        self.assertIn(
            "Draft 2020-12",
            schema["properties"]["json_schema"]["description"],
        )
        self.assertIn(
            "non-streaming",
            schema["properties"]["json_schema"]["description"],
        )
        self.assertIn(
            "local references",
            schema["properties"]["json_schema"]["description"],
        )

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

    def test_supported_request_headers_are_documented(self) -> None:
        for path, method in (
            ("/v1/models", "get"),
            ("/v1/responses", "post"),
            ("/admin/v1/api-keys", "get"),
        ):
            with self.subTest(path=path, method=method):
                header_names = {
                    parameter["name"]
                    for parameter in self.contract["paths"][path][method].get(
                        "parameters", []
                    )
                    if parameter["in"] == "header"
                }
                self.assertIn("X-Request-ID", header_names)
                self.assertIn("traceparent", header_names)
                self.assertNotIn("authorization", {name.lower() for name in header_names})

        for path in ("/v1/responses", "/v1/embeddings"):
            header_names = {
                parameter["name"]
                for parameter in self.contract["paths"][path]["post"]["parameters"]
                if parameter["in"] == "header"
            }
            self.assertIn("Idempotency-Key", header_names)

    def test_openapi_endpoint_serves_the_application_contract(self) -> None:
        response = TestClient(self.app).get("/openapi.json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.contract)

    def test_checked_in_contract_matches_application_contract(self) -> None:
        expected = json.dumps(self.contract, indent=2, sort_keys=True) + "\n"

        self.assertEqual((ROOT / "openapi" / "openapi.json").read_text(), expected)

    def test_live_json_responses_conform_to_contract_models(self) -> None:
        client = self.client
        service_headers = {
            "Authorization": "Bearer dev-key",
            "X-Request-ID": "req_contract_live",
        }
        admin_headers = {"Authorization": "Bearer admin-secret"}

        self._assert_response_model(
            "ModelList", client.get("/v1/models", headers=service_headers)
        )
        self._assert_response_model(
            "ResponseObject",
            client.post(
                "/v1/responses",
                headers=service_headers,
                json={"model": "mixapi/balanced-chat", "input": "hello"},
            ),
        )
        self._assert_response_model(
            "EmbeddingList",
            client.post(
                "/v1/embeddings",
                headers=service_headers,
                json={"model": "mixapi/embedding-small", "input": "hello"},
            ),
        )
        self._assert_response_model(
            "RouteDecision",
            client.get("/v1/route-decisions/req_contract_live", headers=service_headers),
        )
        self._assert_response_model(
            "UsageList", client.get("/v1/usage", headers=service_headers)
        )

        created = client.post(
            "/admin/v1/api-keys",
            headers=admin_headers,
            json={"tenant_id": "tenant_acme", "project_id": "project_chat"},
        )
        self._assert_response_model("ApiKeyCreated", created)
        api_key_id = created.json()["id"]
        self._assert_response_model(
            "ApiKeyList", client.get("/admin/v1/api-keys", headers=admin_headers)
        )
        self._assert_response_model(
            "ApiKeyRecord",
            client.patch(
                f"/admin/v1/api-keys/{api_key_id}",
                headers=admin_headers,
                json={"name": "renamed"},
            ),
        )
        self._assert_response_model(
            "ApiKeyRecord",
            client.delete(f"/admin/v1/api-keys/{api_key_id}", headers=admin_headers),
        )
        self._assert_response_model(
            "TenantPolicy",
            client.put(
                "/admin/v1/tenants/tenant_acme/model-allowlist",
                headers=admin_headers,
                json={"models": ["mixapi/balanced-chat"]},
            ),
        )
        self._assert_response_model(
            "TenantPolicy",
            client.put(
                "/admin/v1/tenants/tenant_acme/routing-policy",
                headers=admin_headers,
                json={"objective": "lowest-cost"},
            ),
        )
        self._assert_response_model(
            "AuditEventList",
            client.get("/admin/v1/audit-events", headers=admin_headers),
        )
        provider_id = self.app.state.configuration_repository.list_providers()[0].id
        candidate_id = self.app.state.configuration_repository.list_candidates()[0].id
        version = self.app.state.snapshot_store.active_version()
        self._assert_response_model(
            "ProviderList",
            client.get("/admin/v1/providers", headers=admin_headers),
        )
        self._assert_response_model(
            "ProviderRecord",
            client.get(f"/admin/v1/providers/{provider_id}", headers=admin_headers),
        )
        self.assertNotIn(
            "credential",
            client.get(f"/admin/v1/providers/{provider_id}", headers=admin_headers).json(),
        )
        self._assert_response_model(
            "LogicalModelList",
            client.get("/admin/v1/models", headers=admin_headers),
        )
        self._assert_response_model(
            "LogicalModelRecord",
            client.get("/admin/v1/models/mixapi/balanced-chat", headers=admin_headers),
        )
        self._assert_response_model(
            "ModelCandidateList",
            client.get("/admin/v1/candidates", headers=admin_headers),
        )
        self._assert_response_model(
            "ModelCandidateRecord",
            client.get(f"/admin/v1/candidates/{candidate_id}", headers=admin_headers),
        )
        self._assert_response_model(
            "ConfigurationStatus",
            client.get(
                f"/admin/v1/configuration/versions/{version}",
                headers=admin_headers,
            ),
        )

    def _assert_response_model(self, model_name: str, response) -> None:
        self.assertLess(response.status_code, 300, response.text)
        CONTRACT_MODELS[model_name].model_validate(response.json())


if __name__ == "__main__":
    unittest.main()
