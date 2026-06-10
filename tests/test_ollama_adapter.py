import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.app_factory import create_app


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


class FakeOllamaServer:
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
                    self.send_header("content-type", "application/x-ndjson")
                    self.end_headers()
                    for chunk in chunks:
                        self.wfile.write(chunk.encode("utf-8"))
                        self.wfile.flush()
                    return
                status, response = owner.responses.get(
                    self.path,
                    (404, {"error": "not found"}),
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


class OllamaAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = FakeOllamaServer()
        self.upstream.start()

    def tearDown(self) -> None:
        self.upstream.stop()

    def test_chat_stream_translates_native_json_lines_and_usage(self) -> None:
        self.upstream.stream_responses["/api/chat"] = (
            200,
            [
                '{"message":{"role":"assistant","content":"Local"},"done":false}\n',
                '{"message":{"role":"assistant","content":" stream"},"done":false}\n',
                '{"message":{"role":"assistant","content":""},"done":true,"prompt_eval_count":3,"eval_count":2}\n',
            ],
        )
        client = TestClient(create_app(ollama_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Stream locally",
                "stream": True,
                "native": {"provider": "ollama", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        events = parse_sse(response.text)
        self.assertEqual(
            [event["data"]["delta"] for event in events if event["event"] == "response.output_text.delta"],
            ["Local", " stream"],
        )
        completed = events[-1]["data"]["response"]
        self.assertEqual(completed["usage"]["input_tokens"], 3)
        self.assertEqual(completed["usage"]["output_tokens"], 2)
        self.assertTrue(self.upstream.requests[0]["body"]["stream"])

    def test_chat_request_uses_configured_ollama_backend(self) -> None:
        self.upstream.responses["/api/chat"] = (
            200,
            {
                "model": "llama3.1",
                "message": {"role": "assistant", "content": "Ollama answer"},
                "done": True,
                "prompt_eval_count": 3,
                "eval_count": 4,
            },
        )
        with patch.dict(
            os.environ,
            {
                "MIXAPI_OLLAMA_BASE_URL": self.upstream.base_url,
                "MIXAPI_OLLAMA_API_KEY": "ollama-key",
            },
            clear=False,
        ):
            client = TestClient(create_app())

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Hello local model",
                "native": {"provider": "ollama", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "ollama")
        self.assertEqual(body["output_text"], "Ollama answer")
        self.assertEqual(body["usage"]["input_tokens"], 3)
        self.assertEqual(body["usage"]["output_tokens"], 4)

        self.assertEqual(len(self.upstream.requests), 1)
        upstream_request = self.upstream.requests[0]
        self.assertEqual(upstream_request["path"], "/api/chat")
        self.assertEqual(upstream_request["authorization"], "Bearer ollama-key")
        self.assertEqual(upstream_request["body"]["model"], "llama3.1")
        self.assertFalse(upstream_request["body"]["stream"])
        self.assertEqual(
            upstream_request["body"]["messages"],
            [{"role": "user", "content": "Hello local model"}],
        )

    def test_chat_request_translates_system_options_and_json_mode(self) -> None:
        self.upstream.responses["/api/chat"] = (
            200,
            {
                "message": {"role": "assistant", "content": '{"ok":true}'},
                "done": True,
                "prompt_eval_count": 8,
                "eval_count": 3,
            },
        )
        client = TestClient(create_app(ollama_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": [
                    {"role": "system", "content": "Return JSON."},
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Is this valid?"}],
                    },
                ],
                "max_output_tokens": 256,
                "response": {"format": {"type": "json_object"}},
                "native": {
                    "provider": "ollama",
                    "provider_options": {
                        "options": {"temperature": 0.2, "num_predict": 999},
                        "keep_alive": "10m",
                    },
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        upstream_body = self.upstream.requests[0]["body"]
        self.assertEqual(
            upstream_body["messages"],
            [
                {"role": "system", "content": "Return JSON."},
                {"role": "user", "content": "Is this valid?"},
            ],
        )
        self.assertEqual(
            upstream_body.get("options"),
            {"temperature": 0.2, "num_predict": 256},
        )
        self.assertEqual(upstream_body.get("format"), "json")
        self.assertEqual(upstream_body.get("keep_alive"), "10m")

    def test_embedding_request_uses_configured_ollama_backend(self) -> None:
        self.upstream.responses["/api/embed"] = (
            200,
            {
                "model": "nomic-embed-text",
                "embeddings": [[0.1, 0.2, 0.3]],
                "prompt_eval_count": 5,
            },
        )
        client = TestClient(create_app(ollama_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/embeddings",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/embedding-small",
                "input": "Embed locally",
                "native": {
                    "provider": "ollama",
                    "provider_options": {"truncate": False},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["provider"], "ollama")
        self.assertEqual(body["data"][0]["embedding"], [0.1, 0.2, 0.3])
        self.assertEqual(body["usage"]["input_tokens"], 5)

        upstream_request = self.upstream.requests[0]
        self.assertEqual(upstream_request["path"], "/api/embed")
        self.assertEqual(upstream_request["body"]["model"], "nomic-embed-text")
        self.assertEqual(upstream_request["body"]["input"], "Embed locally")
        self.assertFalse(upstream_request["body"]["truncate"])

    def test_ollama_base_url_may_include_api_prefix(self) -> None:
        self.upstream.responses["/api/chat"] = (
            200,
            {
                "message": {"role": "assistant", "content": "Prefixed base"},
                "prompt_eval_count": 1,
                "eval_count": 2,
            },
        )
        client = TestClient(create_app(ollama_base_url=f"{self.upstream.base_url}/api"))

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Prefix",
                "native": {"provider": "ollama", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output_text"], "Prefixed base")
        self.assertEqual(self.upstream.requests[0]["path"], "/api/chat")

    def test_malformed_ollama_chat_is_recorded_as_provider_failure(self) -> None:
        self.upstream.responses["/api/chat"] = (
            200,
            {
                "message": {"role": "assistant", "content": None},
                "prompt_eval_count": 2,
                "eval_count": 1,
            },
        )
        client = TestClient(create_app(ollama_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/responses",
            headers={**AUTH_HEADERS, "X-Request-ID": "req_bad_ollama"},
            json={
                "model": "mixapi/balanced-chat",
                "input": "Malformed",
                "native": {"provider": "ollama", "provider_options": {}},
            },
        )
        route = client.get(
            "/v1/route-decisions/req_bad_ollama",
            headers=AUTH_HEADERS,
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(route.status_code, 200)
        self.assertEqual(route.json()["attempts"][0]["reason"], "invalid_upstream_response")

    def test_empty_ollama_chat_is_recorded_as_provider_failure(self) -> None:
        self.upstream.responses["/api/chat"] = (
            200,
            {
                "message": {"role": "assistant", "content": ""},
                "prompt_eval_count": 2,
                "eval_count": 1,
            },
        )
        client = TestClient(create_app(ollama_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/balanced-chat",
                "input": "Empty",
                "native": {"provider": "ollama", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 503)

    def test_empty_ollama_embedding_is_recorded_as_provider_failure(self) -> None:
        self.upstream.responses["/api/embed"] = (
            200,
            {"embeddings": [[]], "prompt_eval_count": 2},
        )
        client = TestClient(create_app(ollama_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/embeddings",
            headers=AUTH_HEADERS,
            json={
                "model": "mixapi/embedding-small",
                "input": "Empty vector",
                "native": {"provider": "ollama", "provider_options": {}},
            },
        )

        self.assertEqual(response.status_code, 503)

    def test_ollama_5xx_is_recorded_as_provider_failure(self) -> None:
        self.upstream.responses["/api/chat"] = (500, {"error": "temporary failure"})
        client = TestClient(create_app(ollama_base_url=self.upstream.base_url))

        response = client.post(
            "/v1/responses",
            headers={**AUTH_HEADERS, "X-Request-ID": "req_ollama_500"},
            json={
                "model": "mixapi/balanced-chat",
                "input": "Fail",
                "native": {"provider": "ollama", "provider_options": {}},
            },
        )
        route = client.get(
            "/v1/route-decisions/req_ollama_500",
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
