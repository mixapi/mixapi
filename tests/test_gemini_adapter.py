import inspect
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from fastapi.testclient import TestClient

from mixapi.app import create_app


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


class FakeGeminiServer:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.responses: dict[str, tuple[int, dict[str, object]]] = {}
        self.stream_responses: dict[str, tuple[int, list[str]]] = {}

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                content_length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(content_length) or b"{}")
                owner.requests.append(
                    {
                        "path": self.path,
                        "api_key": self.headers.get("x-goog-api-key"),
                        "body": body,
                    }
                )
                if self.path in owner.stream_responses:
                    status, chunks = owner.stream_responses[self.path]
                    self.send_response(status)
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    for chunk in chunks:
                        self.wfile.write(chunk.encode("utf-8"))
                        self.wfile.flush()
                    return
                status, response = owner.responses.get(
                    self.path,
                    (404, {"error": {"message": "not found"}}),
                )
                encoded = json.dumps(response).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class GeminiAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = FakeGeminiServer()
        self.upstream.start()

    def tearDown(self) -> None:
        self.upstream.stop()

    def test_generate_content_stream_translates_native_sse_and_usage(self) -> None:
        path = "/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse"
        self.upstream.stream_responses[path] = (
            200,
            [
                'data: {"candidates":[{"content":{"parts":[{"text":"Gemini"}]}}]}\n\n',
                'data: {"candidates":[{"content":{"parts":[{"text":" stream"}]}}],"usageMetadata":{"promptTokenCount":3,"candidatesTokenCount":2}}\n\n',
            ],
        )
        client = TestClient(create_app(gemini_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Stream from Gemini",
                "stream": True,
                "native": {"provider": "gemini", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        events = parse_sse(response.text)
        self.assertEqual(
            [event["data"]["delta"] for event in events if event["event"] == "response.output_text.delta"],
            ["Gemini", " stream"],
        )
        completed = events[-1]["data"]["response"]
        self.assertEqual(completed["usage"]["input_tokens"], 3)
        self.assertEqual(completed["usage"]["output_tokens"], 2)
        self.assertEqual(self.upstream.requests[0]["path"], path)

    def test_generate_content_uses_configured_gemini_backend(self) -> None:
        path = "/v1beta/models/gemini-2.5-flash:generateContent"
        self.upstream.responses[path] = (
            200,
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": "Gemini answer"}],
                        }
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 3,
                    "candidatesTokenCount": 4,
                    "totalTokenCount": 7,
                },
            },
        )
        with patch.dict(
            os.environ,
            {
                "MIXAPI_GEMINI_BASE_URL": self.upstream.base_url,
                "MIXAPI_GEMINI_API_KEY": "gemini-key",
            },
            clear=False,
        ):
            client = TestClient(create_app())

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Hello Gemini",
                "native": {"provider": "gemini", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "gemini")
        self.assertEqual(body["output_text"], "Gemini answer")
        self.assertEqual(body["usage"]["input_tokens"], 3)
        self.assertEqual(body["usage"]["output_tokens"], 4)

        self.assertEqual(len(self.upstream.requests), 1)
        upstream_request = self.upstream.requests[0]
        self.assertEqual(upstream_request["path"], path)
        self.assertEqual(upstream_request["api_key"], "gemini-key")
        self.assertEqual(
            upstream_request["body"]["contents"],
            [{"role": "user", "parts": [{"text": "Hello Gemini"}]}],
        )

    def test_app_accepts_explicit_gemini_configuration(self) -> None:
        path = "/v1beta/models/gemini-2.5-flash:generateContent"
        self.upstream.responses[path] = (
            200,
            {
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": "Explicit config"}]}}
                ],
                "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 2},
            },
        )
        parameters = inspect.signature(create_app).parameters
        self.assertIn("gemini_base_url", parameters)
        client = TestClient(
            create_app(
                gemini_base_url=self.upstream.base_url,
                gemini_api_key="explicit-key",
            )
        )

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Explicit",
                "native": {"provider": "gemini", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output_text"], "Explicit config")
        self.assertEqual(self.upstream.requests[0]["api_key"], "explicit-key")

    def test_gemini_base_url_may_include_v1beta_prefix(self) -> None:
        path = "/v1beta/models/gemini-2.5-flash:generateContent"
        self.upstream.responses[path] = (
            200,
            {
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": "Prefixed base"}]}}
                ],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2},
            },
        )
        client = TestClient(create_app(gemini_base_url=f"{self.upstream.base_url}/v1beta"))

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Prefix",
                "native": {"provider": "gemini", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output_text"], "Prefixed base")
        self.assertEqual(self.upstream.requests[0]["path"], path)

    def test_generate_content_translates_system_roles_and_generation_config(self) -> None:
        path = "/v1beta/models/gemini-2.5-flash:generateContent"
        self.upstream.responses[path] = (
            200,
            {
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": '{"ok":true}'}]}}
                ],
                "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 3},
            },
        )
        with patch.dict(
            os.environ,
            {"MIXAPI_GEMINI_BASE_URL": self.upstream.base_url},
            clear=False,
        ):
            client = TestClient(create_app())
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        }

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": [
                    {
                        "role": "system",
                        "content": [{"type": "input_text", "text": "Return JSON."}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Is this valid?"}],
                    },
                    {
                        "role": "assistant",
                        "content": [{"type": "input_text", "text": "Checking."}],
                    },
                ],
                "max_output_tokens": 256,
                "response": {
                    "format": {"type": "json_schema", "json_schema": schema}
                },
                "native": {
                    "provider": "gemini",
                    "provider_options": {
                        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 999}
                    },
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        upstream_body = self.upstream.requests[0]["body"]
        self.assertEqual(
            upstream_body.get("systemInstruction"),
            {"parts": [{"text": "Return JSON."}]},
        )
        self.assertEqual(
            upstream_body["contents"],
            [
                {"role": "user", "parts": [{"text": "Is this valid?"}]},
                {"role": "model", "parts": [{"text": "Checking."}]},
            ],
        )
        self.assertEqual(
            upstream_body.get("generationConfig"),
            {
                "temperature": 0.2,
                "maxOutputTokens": 256,
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
            },
        )

    def test_generate_content_translates_and_normalizes_function_tools(self) -> None:
        path = "/v1beta/models/gemini-2.5-flash:generateContent"
        self.upstream.responses[path] = (
            200,
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {"text": "I will look that up."},
                                {
                                    "functionCall": {
                                        "name": "lookup_invoice",
                                        "args": {"invoice_id": "inv_123"},
                                    }
                                },
                            ],
                        }
                    }
                ],
                "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 5},
            },
        )
        with patch.dict(
            os.environ,
            {"MIXAPI_GEMINI_BASE_URL": self.upstream.base_url},
            clear=False,
        ):
            client = TestClient(create_app())
        parameters = {
            "type": "object",
            "properties": {"invoice_id": {"type": "string"}},
            "required": ["invoice_id"],
        }

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Find invoice inv_123",
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup_invoice",
                        "description": "Look up an invoice by ID.",
                        "parameters": parameters,
                    }
                ],
                "tool_choice": "lookup_invoice",
                "native": {"provider": "gemini", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        upstream_body = self.upstream.requests[0]["body"]
        self.assertEqual(
            upstream_body.get("tools"),
            [
                {
                    "functionDeclarations": [
                        {
                            "name": "lookup_invoice",
                            "description": "Look up an invoice by ID.",
                            "parameters": parameters,
                        }
                    ]
                }
            ],
        )
        self.assertEqual(
            upstream_body.get("toolConfig"),
            {
                "functionCallingConfig": {
                    "mode": "ANY",
                    "allowedFunctionNames": ["lookup_invoice"],
                }
            },
        )
        body = response.json()
        self.assertEqual(body["output_text"], "I will look that up.")
        self.assertEqual(
            body["output"],
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "I will look that up."}],
                },
                {
                    "type": "function_call",
                    "id": "call_gemini_1",
                    "name": "lookup_invoice",
                    "arguments": '{"invoice_id":"inv_123"}',
                },
            ],
        )

    def test_empty_gemini_candidate_is_recorded_as_provider_failure(self) -> None:
        path = "/v1beta/models/gemini-2.5-flash:generateContent"
        self.upstream.responses[path] = (
            200,
            {
                "candidates": [{"content": {"role": "model", "parts": [{}]}}],
                "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1},
            },
        )
        client = TestClient(create_app(gemini_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/responses",
            headers={**AUTH_HEADERS, "X-Request-ID": "req_bad_gemini"},
            json={
                "model": "mixapi/balanced-chat",
                "input": "Malformed",
                "native": {"provider": "gemini", "provider_options": {}},
            },
        )
        route = client.get(
            "/v1/route-decisions/req_bad_gemini",
            headers=AUTH_HEADERS,
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["type"], "provider_unavailable")
        self.assertEqual(route.status_code, 200)
        self.assertEqual(route.json()["attempts"][0]["reason"], "invalid_upstream_response")

    def test_gemini_5xx_is_recorded_as_provider_failure(self) -> None:
        path = "/v1beta/models/gemini-2.5-flash:generateContent"
        self.upstream.responses[path] = (500, {"error": {"message": "temporary failure"}})
        client = TestClient(create_app(gemini_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/responses",
            headers={**AUTH_HEADERS, "X-Request-ID": "req_gemini_500"},
            json={
                "model": "mixapi/balanced-chat",
                "input": "Fail",
                "native": {"provider": "gemini", "provider_options": {}},
            },
        )
        route = client.get(
            "/v1/route-decisions/req_gemini_500",
            headers=AUTH_HEADERS,
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(route.status_code, 200)
        self.assertEqual(route.json()["attempts"][0]["reason"], "upstream_http_500")


def parse_sse(raw: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for block in raw.strip().split("\n\n"):
        event_name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = line.removeprefix("data: ")
        events.append({"event": event_name, "data": json.loads(data)})
    return events


if __name__ == "__main__":
    unittest.main()
