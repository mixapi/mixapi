import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from mixapi.app import create_app


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


class FakeOpenAIServer:
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
                        "authorization": self.headers.get("authorization"),
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


class OpenAICompatibleAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = FakeOpenAIServer()
        self.upstream.start()

    def tearDown(self) -> None:
        self.upstream.stop()

    def test_chat_stream_uses_native_sse_and_preserves_delta_order(self) -> None:
        self.upstream.stream_responses["/v1/chat/completions"] = (
            200,
            [
                'data: {"choices":[{"delta":{"content":"Open"}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"AI"}}]}\n\n',
                'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n',
                "data: [DONE]\n\n",
            ],
        )
        client = TestClient(create_app(openai_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Stream from OpenAI",
                "stream": True,
                "native": {"provider": "openai", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [event["data"]["delta"] for event in parse_sse(response.text) if event["event"] == "response.output_text.delta"],
            ["Open", "AI"],
        )
        completed = parse_sse(response.text)[-1]["data"]["response"]
        self.assertEqual(completed["output_text"], "OpenAI")
        self.assertEqual(completed["usage"]["input_tokens"], 2)
        self.assertEqual(completed["usage"]["output_tokens"], 1)
        self.assertTrue(self.upstream.requests[0]["body"]["stream"])
        self.assertEqual(
            self.upstream.requests[0]["body"]["stream_options"],
            {"include_usage": True},
        )

    def test_stream_failure_before_first_delta_falls_back(self) -> None:
        self.upstream.stream_responses["/v1/chat/completions"] = (
            200,
            ["data: not-json\n\n"],
        )
        client = TestClient(
            create_app(
                openai_base_url=self.upstream.base_url,
                failed_response_providers={"ollama"},
            )
        )

        response = client.post(
            "/v1/responses",
            headers={**AUTH_HEADERS, "X-Request-ID": "req_stream_prime_failure"},
            json={
                "model": "mixapi/balanced-chat",
                "input": "Fallback before output",
                "stream": True,
                "routing": {"objective": "lowest-cost"},
            },
        )

        self.assertEqual(response.status_code, 200)
        events = parse_sse(response.text)
        self.assertEqual(events[0]["data"]["provider"], "gemini")
        completed = events[-1]["data"]["response"]
        self.assertTrue(completed["route"]["fallback_used"])
        self.assertEqual(completed["route"]["attempts"], 3)

    def test_stream_failure_after_first_delta_does_not_fall_back(self) -> None:
        self.upstream.stream_responses["/v1/chat/completions"] = (
            200,
            [
                'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
                "data: not-json\n\n",
            ],
        )
        app = create_app(
            openai_base_url=self.upstream.base_url,
            failed_response_providers={"ollama"},
        )
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers={**AUTH_HEADERS, "X-Request-ID": "req_stream_interrupted"},
            json={
                "model": "mixapi/balanced-chat",
                "input": "Do not switch providers",
                "stream": True,
                "routing": {"objective": "lowest-cost"},
            },
        )

        self.assertEqual(response.status_code, 200)
        events = parse_sse(response.text)
        self.assertEqual(
            [event["event"] for event in events],
            ["response.created", "response.output_text.delta", "response.failed"],
        )
        self.assertEqual(events[-1]["data"]["error"]["type"], "stream_interrupted")
        route = client.get(
            "/v1/route-decisions/req_stream_interrupted",
            headers=AUTH_HEADERS,
        ).json()
        self.assertEqual(route["status"], "failed")
        self.assertEqual(route["selected_provider"], "openai")
        self.assertEqual(len(route["attempts"]), 2)
        self.assertEqual(len(app.state.usage_ledger.events()), 1)

    def test_chat_request_uses_configured_openai_compatible_backend(self) -> None:
        self.upstream.responses["/v1/chat/completions"] = (
            200,
            {
                "id": "chatcmpl_upstream",
                "choices": [{"message": {"role": "assistant", "content": "Upstream answer"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
            },
        )
        app = create_app(
            openai_base_url=self.upstream.base_url,
            openai_api_key="upstream-key",
        )
        client = TestClient(app)

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Hello upstream"}],
                    }
                ],
                "native": {"provider": "openai", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "openai")
        self.assertEqual(body["output_text"], "Upstream answer")
        self.assertEqual(body["usage"]["input_tokens"], 2)
        self.assertEqual(body["usage"]["output_tokens"], 3)

        self.assertEqual(len(self.upstream.requests), 1)
        upstream_request = self.upstream.requests[0]
        self.assertEqual(upstream_request["path"], "/v1/chat/completions")
        self.assertEqual(upstream_request["authorization"], "Bearer upstream-key")
        self.assertEqual(upstream_request["body"]["model"], "gpt-4.1-mini")
        self.assertEqual(
            upstream_request["body"]["messages"],
            [{"role": "user", "content": "Hello upstream"}],
        )

    def test_base_url_may_include_v1_prefix(self) -> None:
        self.upstream.responses["/v1/chat/completions"] = (
            200,
            {
                "choices": [{"message": {"role": "assistant", "content": "Prefixed base"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            },
        )
        client = TestClient(create_app(openai_base_url=f"{self.upstream.base_url}/v1"))

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Prefix",
                "native": {"provider": "openai", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output_text"], "Prefixed base")
        self.assertEqual(self.upstream.requests[0]["path"], "/v1/chat/completions")

    def test_chat_request_translates_tools_choice_and_json_schema(self) -> None:
        self.upstream.responses["/v1/chat/completions"] = (
            200,
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"status":"found"}',
                        }
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2},
            },
        )
        client = TestClient(create_app(openai_base_url=self.upstream.base_url))
        invoice_schema = {
            "type": "object",
            "properties": {"invoice_id": {"type": "string"}},
            "required": ["invoice_id"],
            "additionalProperties": False,
        }
        output_schema = {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
            "additionalProperties": False,
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
                        "parameters": invoice_schema,
                    }
                ],
                "tool_choice": "lookup_invoice",
                "response": {
                    "format": {
                        "type": "json_schema",
                        "json_schema": output_schema,
                    }
                },
                "native": {
                    "provider": "openai",
                    "provider_options": {"temperature": 0},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output_text"], '{"status":"found"}')
        upstream_body = self.upstream.requests[0]["body"]
        self.assertEqual(upstream_body["temperature"], 0)
        self.assertEqual(
            upstream_body["tools"],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup_invoice",
                        "description": "Look up an invoice by ID.",
                        "parameters": invoice_schema,
                    },
                }
            ],
        )
        self.assertEqual(
            upstream_body["tool_choice"],
            {"type": "function", "function": {"name": "lookup_invoice"}},
        )
        self.assertEqual(
            upstream_body["response_format"],
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "mixapi_response",
                    "schema": output_schema,
                    "strict": True,
                },
            },
        )

    def test_chat_tool_call_is_normalized_as_function_call_output(self) -> None:
        self.upstream.responses["/v1/chat/completions"] = (
            200,
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_invoice_1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup_invoice",
                                        "arguments": '{"invoice_id":"inv_123"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4},
            },
        )
        client = TestClient(create_app(openai_base_url=self.upstream.base_url))

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
                        "parameters": {
                            "type": "object",
                            "properties": {"invoice_id": {"type": "string"}},
                        },
                    }
                ],
                "native": {"provider": "openai", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["output_text"], "")
        self.assertEqual(
            body["output"],
            [
                {
                    "type": "function_call",
                    "id": "call_invoice_1",
                    "name": "lookup_invoice",
                    "arguments": '{"invoice_id":"inv_123"}',
                }
            ],
        )
        self.assertEqual(body["usage"]["input_tokens"], 9)
        self.assertEqual(body["usage"]["output_tokens"], 4)

    def test_embedding_request_uses_configured_openai_compatible_backend(self) -> None:
        self.upstream.responses["/v1/embeddings"] = (
            200,
            {
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            },
        )
        client = TestClient(
            create_app(
                openai_base_url=self.upstream.base_url,
                openai_api_key="upstream-key",
            )
        )

        response = client.post(
            "/v1/embeddings",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/embedding-small",
                "input": "Embed upstream",
                "native": {"provider": "openai", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "openai")
        self.assertEqual(body["data"][0]["embedding"], [0.1, 0.2, 0.3])
        self.assertEqual(body["usage"]["input_tokens"], 4)

        upstream_request = self.upstream.requests[0]
        self.assertEqual(upstream_request["path"], "/v1/embeddings")
        self.assertEqual(upstream_request["authorization"], "Bearer upstream-key")
        self.assertEqual(upstream_request["body"]["model"], "text-embedding-3-small")
        self.assertEqual(upstream_request["body"]["input"], "Embed upstream")

    def test_upstream_5xx_falls_back_to_next_eligible_provider(self) -> None:
        self.upstream.responses["/v1/chat/completions"] = (
            500,
            {"error": {"message": "temporary failure"}},
        )
        client = TestClient(create_app(openai_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Fallback from HTTP",
                "routing": {"objective": "highest-reliability"},
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "gemini")
        self.assertEqual(body["route"]["attempts"], 2)
        self.assertTrue(body["route"]["fallback_used"])
        self.assertEqual(body["route"]["failed_attempts"][0]["provider"], "openai")
        self.assertEqual(
            body["route"]["failed_attempts"][0]["reason"],
            "upstream_http_500",
        )

    def test_malformed_upstream_response_is_recorded_as_provider_failure(self) -> None:
        self.upstream.responses["/v1/chat/completions"] = (200, {"choices": []})
        client = TestClient(create_app(openai_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/responses",
            headers={**AUTH_HEADERS, "X-Request-ID": "req_bad_upstream"},
            json={
                "model": "mixapi/balanced-chat",
                "input": "Malformed response",
                "native": {"provider": "openai", "provider_options": {}},
            },
        )
        route = client.get(
            "/v1/route-decisions/req_bad_upstream",
            headers=AUTH_HEADERS,
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["type"], "provider_unavailable")
        self.assertEqual(route.status_code, 200)
        self.assertEqual(route.json()["attempts"][0]["reason"], "invalid_upstream_response")

    def test_embedding_upstream_failure_falls_back_to_local_candidate(self) -> None:
        self.upstream.responses["/v1/embeddings"] = (
            500,
            {"error": {"message": "embedding backend unavailable"}},
        )
        client = TestClient(create_app(openai_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/embeddings",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/embedding-small",
                "input": "Fallback embedding",
                "routing": {"objective": "balanced"},
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "ollama")
        self.assertEqual(body["route"]["attempts"], 2)
        self.assertTrue(body["route"]["fallback_used"])


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
