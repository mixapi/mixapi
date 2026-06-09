import unittest

from fastapi.testclient import TestClient

from mixapi.app import create_app


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


class RequestValidationTest(unittest.TestCase):
    def test_response_requires_non_empty_text_input(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={"model": "mixapi/balanced-chat", "input": []},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["type"], "validation_error")
        self.assertEqual(response.json()["error"]["code"], "input_required")
        self.assertEqual(len(app.state.usage_ledger.events()), 0)

    def test_embedding_requires_non_empty_input(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.post(
            "/v1/embeddings",
            headers=AUTH_HEADERS,
            json={"model": "mixapi/embedding-small", "input": "   "},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "input_required")
        self.assertEqual(len(app.state.usage_ledger.events()), 0)

    def test_stream_must_be_boolean(self) -> None:
        client = TestClient(create_app())

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Hello",
                "stream": "true",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_stream")

    def test_streaming_rejects_idempotency_key(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers={**AUTH_HEADERS, "Idempotency-Key": "stream-key"},
            json={
                "model": "mixapi/balanced-chat",
                "input": "Hello",
                "stream": True,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "streaming_idempotency_unsupported")
        self.assertEqual(len(app.state.usage_ledger.events()), 0)

    def test_streaming_rejects_tools_until_tool_deltas_are_supported(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Look up an invoice",
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup_invoice",
                        "parameters": {"type": "object"},
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["type"], "capability_unsupported")
        self.assertEqual(response.json()["error"]["code"], "streaming_tools_unsupported")
        self.assertEqual(len(app.state.usage_ledger.events()), 0)

    def test_response_rejects_unsupported_tool_definition(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Search for an invoice",
                "tools": [{"type": "web_search", "name": "search"}],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_tools")
        self.assertEqual(len(app.state.usage_ledger.events()), 0)

    def test_response_rejects_invalid_max_output_tokens(self) -> None:
        app = create_app()
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Hello",
                "max_output_tokens": True,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_max_output_tokens")
        self.assertEqual(len(app.state.usage_ledger.events()), 0)


if __name__ == "__main__":
    unittest.main()
