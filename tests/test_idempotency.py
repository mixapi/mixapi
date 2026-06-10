import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mixapi.adapters import AdapterResponse, DeterministicProviderAdapter
from mixapi.app import create_app


AUTH_HEADERS = {
    "Authorization": "Bearer dev-key",
    "Idempotency-Key": "idem-1",
}


class IdempotencyTest(unittest.TestCase):
    def test_repeated_response_idempotency_key_replays_first_response(self) -> None:
        app = create_app()
        client = TestClient(app)
        payload = {
            "model": "mixapi/balanced-chat",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "Replay me"}]}],
        }

        first = client.post("/v1/responses", headers=AUTH_HEADERS, json=payload)
        second = client.post("/v1/responses", headers=AUTH_HEADERS, json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(len(app.state.usage_ledger.events()), 1)

    def test_idempotency_key_reuse_with_different_body_returns_validation_error(self) -> None:
        app = create_app()
        client = TestClient(app)

        first = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "First"}]}],
            },
        )
        second = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Different"}]}],
            },
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()["error"]["type"], "validation_error")
        self.assertEqual(second.json()["error"]["code"], "idempotency_key_reused")
        self.assertEqual(len(app.state.usage_ledger.events()), 1)

    def test_repeated_embedding_idempotency_key_replays_first_response(self) -> None:
        app = create_app()
        client = TestClient(app)
        payload = {"model": "mixapi/embedding-small", "input": "Replay embedding"}

        first = client.post("/v1/embeddings", headers=AUTH_HEADERS, json=payload)
        second = client.post("/v1/embeddings", headers=AUTH_HEADERS, json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(len(app.state.usage_ledger.events()), 1)

    def test_structured_response_replay_does_not_redispatch_or_revalidate(self) -> None:
        responses = iter(
            (
                AdapterResponse("not-json", 2, 3),
                AdapterResponse('{"ok":true}', 4, 5),
            )
        )
        app = create_app()
        client = TestClient(app)
        payload = {
            "model": "mixapi/balanced-chat",
            "input": "Return JSON",
            "native": {"provider": "openai"},
            "response": {
                "format": {
                    "type": "json_schema",
                    "json_schema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    },
                }
            },
        }

        with patch.object(
            DeterministicProviderAdapter,
            "dispatch_response",
            autospec=True,
            side_effect=lambda *_args: next(responses),
        ) as dispatch:
            first = client.post("/v1/responses", headers=AUTH_HEADERS, json=payload)
            second = client.post("/v1/responses", headers=AUTH_HEADERS, json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(len(app.state.usage_ledger.events()), 2)
        validation_spans = [
            span
            for span in app.state.observability.spans()
            if span.name == "structured_output.validate"
        ]
        self.assertEqual(len(validation_spans), 2)


if __name__ == "__main__":
    unittest.main()
