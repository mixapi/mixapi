import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mixapi.adapters import DeterministicProviderAdapter
from tests.app_factory import create_app


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

    def test_response_rejects_malformed_json_schema_before_dispatch(self) -> None:
        client = TestClient(create_app())

        with patch.object(
            DeterministicProviderAdapter,
            "dispatch_response",
            autospec=True,
        ) as dispatch:
            response = client.post(
                "/v1/responses",
                headers=AUTH_HEADERS,
                json=_structured_request({"type": 7}),
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["type"], "validation_error")
        self.assertEqual(response.json()["error"]["code"], "invalid_json_schema")
        dispatch.assert_not_called()

    def test_response_rejects_non_object_json_schema_before_dispatch(self) -> None:
        client = TestClient(create_app())

        with patch.object(
            DeterministicProviderAdapter,
            "dispatch_response",
            autospec=True,
        ) as dispatch:
            response = client.post(
                "/v1/responses",
                headers=AUTH_HEADERS,
                json=_structured_request("not-an-object"),
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_json_schema")
        dispatch.assert_not_called()

    def test_response_rejects_streaming_json_schema_before_dispatch(self) -> None:
        request_body = _structured_request({"type": "object"})
        request_body["stream"] = True
        client = TestClient(create_app())

        with patch.object(
            DeterministicProviderAdapter,
            "start_response_stream",
            autospec=True,
        ) as start_stream:
            response = client.post(
                "/v1/responses",
                headers=AUTH_HEADERS,
                json=request_body,
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["type"], "capability_unsupported")
        self.assertEqual(
            response.json()["error"]["code"],
            "streaming_structured_output_unsupported",
        )
        start_stream.assert_not_called()


def _structured_request(schema) -> dict[str, object]:
    return {
        "model": "mixapi/balanced-chat",
        "input": "Return JSON",
        "response": {
            "format": {
                "type": "json_schema",
                "json_schema": schema,
            }
        },
    }


if __name__ == "__main__":
    unittest.main()
