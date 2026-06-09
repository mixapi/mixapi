import unittest

from fastapi.testclient import TestClient

from mixapi.app import create_app


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


class ResponsesEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app())

    def test_lowest_cost_response_routes_to_local_candidate(self) -> None:
        response = self.client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}],
                "routing": {"objective": "lowest-cost"},
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["model"], "mixapi/balanced-chat")
        self.assertEqual(body["provider"], "ollama")
        self.assertIn("Hello", body["output_text"])
        self.assertEqual(body["cost"]["provider_cost_usd"], "0.00000000")
        self.assertEqual(body["route"]["attempts"], 1)
        self.assertFalse(body["route"]["fallback_used"])

    def test_provider_pin_limits_routing_candidates(self) -> None:
        response = self.client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}],
                "native": {"provider": "anthropic", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "anthropic")
        self.assertEqual(body["route"]["selected_provider_model"], "claude-sonnet-4")

    def test_strict_schema_routes_to_strict_schema_candidate(self) -> None:
        response = self.client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Return JSON"}]}],
                "response": {
                    "format": {
                        "type": "json_schema",
                        "json_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                    }
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "openai")
        self.assertEqual(body["route"]["selected_provider_model"], "gpt-4.1-mini")

    def test_tool_request_excludes_candidates_without_function_tool_support(self) -> None:
        response = self.client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Look up invoice inv_123",
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup_invoice",
                        "description": "Look up an invoice by ID.",
                        "parameters": {
                            "type": "object",
                            "properties": {"invoice_id": {"type": "string"}},
                            "required": ["invoice_id"],
                        },
                    }
                ],
                "routing": {"objective": "lowest-cost"},
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "openai")
        self.assertIn(
            {
                "provider": "ollama",
                "model": "llama3.1",
                "reason": "tools_not_supported",
            },
            body["route"]["rejected_candidates"],
        )

    def test_unsupported_feature_returns_capability_error(self) -> None:
        response = self.client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Describe this"},
                            {"type": "input_image", "image_url": "https://example.com/image.png"},
                        ],
                    }
                ],
                "native": {"provider": "ollama", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"]["type"], "capability_unsupported")
        self.assertEqual(body["error"]["code"], "missing_input_modality")

    def test_gemini_media_input_is_rejected_until_adapter_supports_file_translation(self) -> None:
        response = self.client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Describe this"},
                            {
                                "type": "input_image",
                                "image_url": "https://example.com/image.png",
                            },
                        ],
                    }
                ],
                "native": {"provider": "gemini", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "missing_input_modality")


if __name__ == "__main__":
    unittest.main()
