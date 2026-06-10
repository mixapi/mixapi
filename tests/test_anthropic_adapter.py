import inspect
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.app_factory import create_app


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


class FakeAnthropicServer:
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
                        "api_key": self.headers.get("x-api-key"),
                        "anthropic_version": self.headers.get("anthropic-version"),
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
                    (404, {"type": "error", "error": {"message": "not found"}}),
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


class AnthropicAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = FakeAnthropicServer()
        self.upstream.start()

    def tearDown(self) -> None:
        self.upstream.stop()

    def test_messages_stream_translates_native_events_and_usage(self) -> None:
        self.upstream.stream_responses["/v1/messages"] = (
            200,
            [
                'event: message_start\ndata: {"message":{"usage":{"input_tokens":3}}}\n\n',
                'event: content_block_delta\ndata: {"delta":{"type":"text_delta","text":"Claude"}}\n\n',
                'event: content_block_delta\ndata: {"delta":{"type":"text_delta","text":" stream"}}\n\n',
                'event: message_delta\ndata: {"usage":{"output_tokens":2}}\n\n',
                "event: message_stop\ndata: {}\n\n",
            ],
        )
        client = TestClient(create_app(anthropic_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Stream from Claude",
                "stream": True,
                "native": {"provider": "anthropic", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        events = parse_sse(response.text)
        self.assertEqual(
            [event["data"]["delta"] for event in events if event["event"] == "response.output_text.delta"],
            ["Claude", " stream"],
        )
        completed = events[-1]["data"]["response"]
        self.assertEqual(completed["usage"]["input_tokens"], 3)
        self.assertEqual(completed["usage"]["output_tokens"], 2)
        self.assertTrue(self.upstream.requests[0]["body"]["stream"])

    def test_messages_request_uses_configured_anthropic_backend(self) -> None:
        self.upstream.responses["/v1/messages"] = (
            200,
            {
                "id": "msg_upstream",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Anthropic answer"}],
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        )
        environment = {
            "MIXAPI_ANTHROPIC_BASE_URL": self.upstream.base_url,
            "MIXAPI_ANTHROPIC_API_KEY": "anthropic-key",
        }
        with patch.dict(os.environ, environment, clear=False):
            client = TestClient(create_app())

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Hello Claude",
                "native": {"provider": "anthropic", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "anthropic")
        self.assertEqual(body["output_text"], "Anthropic answer")
        self.assertEqual(body["usage"]["input_tokens"], 3)
        self.assertEqual(body["usage"]["output_tokens"], 4)

        self.assertEqual(len(self.upstream.requests), 1)
        upstream_request = self.upstream.requests[0]
        self.assertEqual(upstream_request["path"], "/v1/messages")
        self.assertEqual(upstream_request["api_key"], "anthropic-key")
        self.assertEqual(upstream_request["anthropic_version"], "2023-06-01")
        self.assertEqual(upstream_request["body"]["model"], "claude-sonnet-4")
        self.assertEqual(upstream_request["body"]["max_tokens"], 1024)
        self.assertEqual(
            upstream_request["body"]["messages"],
            [{"role": "user", "content": "Hello Claude"}],
        )

    def test_app_accepts_explicit_anthropic_configuration(self) -> None:
        self.upstream.responses["/v1/messages"] = (
            200,
            {
                "content": [{"type": "text", "text": "Explicit config"}],
                "usage": {"input_tokens": 2, "output_tokens": 2},
            },
        )
        parameters = inspect.signature(create_app).parameters
        self.assertIn("anthropic_base_url", parameters)
        client = TestClient(
            create_app(
                anthropic_base_url=self.upstream.base_url,
                anthropic_api_key="explicit-key",
                anthropic_version="2023-06-01",
            )
        )

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Explicit",
                "native": {"provider": "anthropic", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output_text"], "Explicit config")
        self.assertEqual(self.upstream.requests[0]["api_key"], "explicit-key")

    def test_messages_request_translates_system_image_and_output_limit(self) -> None:
        self.upstream.responses["/v1/messages"] = (
            200,
            {
                "content": [{"type": "text", "text": "An invoice image"}],
                "usage": {"input_tokens": 10, "output_tokens": 3},
            },
        )
        with patch.dict(
            os.environ,
            {"MIXAPI_ANTHROPIC_BASE_URL": self.upstream.base_url},
            clear=False,
        ):
            client = TestClient(create_app())

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": [
                    {
                        "role": "system",
                        "content": [{"type": "input_text", "text": "Be concise."}],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Describe this invoice"},
                            {
                                "type": "input_image",
                                "image_url": "https://example.com/invoice.png",
                            },
                        ],
                    },
                ],
                "max_output_tokens": 256,
                "native": {
                    "provider": "anthropic",
                    "provider_options": {"temperature": 0.2},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        upstream_body = self.upstream.requests[0]["body"]
        self.assertEqual(upstream_body.get("system"), "Be concise.")
        self.assertEqual(upstream_body["max_tokens"], 256)
        self.assertEqual(upstream_body["temperature"], 0.2)
        self.assertEqual(
            upstream_body["messages"],
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this invoice"},
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": "https://example.com/invoice.png",
                            },
                        },
                    ],
                }
            ],
        )

    def test_messages_request_translates_and_normalizes_function_tools(self) -> None:
        self.upstream.responses["/v1/messages"] = (
            200,
            {
                "content": [
                    {"type": "text", "text": "I will look that up."},
                    {
                        "type": "tool_use",
                        "id": "toolu_invoice_1",
                        "name": "lookup_invoice",
                        "input": {"invoice_id": "inv_123"},
                    },
                ],
                "usage": {"input_tokens": 12, "output_tokens": 6},
            },
        )
        with patch.dict(
            os.environ,
            {"MIXAPI_ANTHROPIC_BASE_URL": self.upstream.base_url},
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
                "native": {"provider": "anthropic", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        upstream_body = self.upstream.requests[0]["body"]
        self.assertEqual(
            upstream_body.get("tools"),
            [
                {
                    "name": "lookup_invoice",
                    "description": "Look up an invoice by ID.",
                    "input_schema": parameters,
                }
            ],
        )
        self.assertEqual(
            upstream_body.get("tool_choice"),
            {"type": "tool", "name": "lookup_invoice"},
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
                    "id": "toolu_invoice_1",
                    "name": "lookup_invoice",
                    "arguments": '{"invoice_id":"inv_123"}',
                },
            ],
        )

    def test_malformed_anthropic_content_is_recorded_as_provider_failure(self) -> None:
        self.upstream.responses["/v1/messages"] = (
            200,
            {
                "content": [{"type": "text"}],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )
        client = TestClient(create_app(anthropic_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/responses",
            headers={**AUTH_HEADERS, "X-Request-ID": "req_bad_anthropic"},
            json={
                "model": "mixapi/balanced-chat",
                "input": "Malformed",
                "native": {"provider": "anthropic", "provider_options": {}},
            },
        )
        route = client.get(
            "/v1/route-decisions/req_bad_anthropic",
            headers=AUTH_HEADERS,
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["type"], "provider_unavailable")
        self.assertEqual(route.status_code, 200)
        self.assertEqual(route.json()["attempts"][0]["reason"], "invalid_upstream_response")

    def test_anthropic_5xx_is_recorded_as_provider_failure(self) -> None:
        self.upstream.responses["/v1/messages"] = (
            500,
            {"type": "error", "error": {"message": "temporary failure"}},
        )
        client = TestClient(create_app(anthropic_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/responses",
            headers={**AUTH_HEADERS, "X-Request-ID": "req_anthropic_500"},
            json={
                "model": "mixapi/balanced-chat",
                "input": "Fail",
                "native": {"provider": "anthropic", "provider_options": {}},
            },
        )
        route = client.get(
            "/v1/route-decisions/req_anthropic_500",
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
