from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import quote

import httpx

from mixapi.models import ProviderModel


class ProviderDispatchError(Exception):
    def __init__(self, provider: str, provider_model_id: str, reason: str) -> None:
        super().__init__(reason)
        self.provider = provider
        self.provider_model_id = provider_model_id
        self.reason = reason


@dataclass(frozen=True)
class AdapterResponse:
    output_text: str
    input_tokens: int
    output_tokens: int
    embedding: list[float] | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ProviderStreamEvent:
    delta: str | None = None
    completed: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None


class ProviderStream:
    def __init__(
        self,
        events: Iterator[ProviderStreamEvent],
        close: Callable[[], None],
        candidate: ProviderModel,
    ) -> None:
        self._events = events
        self._close = close
        self._closed = False
        try:
            self._first_event = next(self._events)
        except StopIteration as error:
            self.close()
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            ) from error
        except BaseException:
            self.close()
            raise

    def __iter__(self) -> Iterator[ProviderStreamEvent]:
        try:
            yield self._first_event
            yield from self._events
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close()


class DeterministicProviderAdapter:
    def __init__(self, failed_response_providers: set[str] | None = None) -> None:
        self._failed_response_providers = failed_response_providers or set()

    def dispatch_response(self, request_body: dict[str, Any], candidate: ProviderModel) -> AdapterResponse:
        if candidate.provider in self._failed_response_providers:
            raise ProviderDispatchError(
                provider=candidate.provider,
                provider_model_id=candidate.provider_model_id,
                reason="configured_provider_failure",
            )

        prompt = extract_text(request_body.get("input", []))
        if _requests_json_schema(request_body):
            output_text = "{}"
        else:
            output_text = f"[{candidate.provider}:{candidate.provider_model_id}] {prompt}".strip()
        return AdapterResponse(
            output_text=output_text,
            input_tokens=count_tokens(prompt),
            output_tokens=count_tokens(output_text),
        )

    def dispatch_embedding(self, request_body: dict[str, Any], candidate: ProviderModel) -> AdapterResponse:
        text = embedding_text(request_body.get("input", ""))
        return AdapterResponse(
            output_text="",
            input_tokens=count_tokens(text),
            output_tokens=0,
            embedding=deterministic_embedding(text, candidate.provider_model_id),
        )

    def start_response_stream(
        self,
        request_body: dict[str, Any],
        candidate: ProviderModel,
    ) -> ProviderStream:
        response = self.dispatch_response(request_body, candidate)

        def events() -> Iterator[ProviderStreamEvent]:
            for delta in _text_deltas(response.output_text):
                yield ProviderStreamEvent(delta=delta)
            yield ProviderStreamEvent(
                completed=True,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )

        return ProviderStream(events(), lambda: None, candidate)


class OpenAICompatibleProviderAdapter:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def dispatch_response(self, request_body: dict[str, Any], candidate: ProviderModel) -> AdapterResponse:
        payload = _provider_options(request_body)
        payload.update(_openai_portable_options(request_body))
        payload.update(
            {
                "model": candidate.provider_model_id,
                "messages": _chat_messages(request_body.get("input")),
                "stream": False,
            }
        )

        data = self._post_json("/v1/chat/completions", payload, candidate)
        try:
            message = data["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError("chat message must be an object")
            output_text = _chat_output_text(message)
            tool_calls = _chat_tool_calls(message)
            usage = data.get("usage", {})
            input_tokens = int(usage.get("prompt_tokens", 0))
            output_tokens = int(usage.get("completion_tokens", 0))
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            ) from error

        return AdapterResponse(
            output_text=output_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
        )

    def dispatch_embedding(self, request_body: dict[str, Any], candidate: ProviderModel) -> AdapterResponse:
        payload = {
            "model": candidate.provider_model_id,
            "input": request_body.get("input"),
        }
        payload.update(_provider_options(request_body))
        payload["model"] = candidate.provider_model_id
        payload["input"] = request_body.get("input")

        data = self._post_json("/v1/embeddings", payload, candidate)
        try:
            embedding = [float(value) for value in data["data"][0]["embedding"]]
            usage = data.get("usage", {})
            input_tokens = int(usage.get("prompt_tokens", usage.get("total_tokens", 0)))
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            ) from error

        return AdapterResponse(
            output_text="",
            input_tokens=input_tokens,
            output_tokens=0,
            embedding=embedding,
        )

    def start_response_stream(
        self,
        request_body: dict[str, Any],
        candidate: ProviderModel,
    ) -> ProviderStream:
        payload = _provider_options(request_body)
        payload.update(_openai_portable_options(request_body))
        payload.update(
            {
                "model": candidate.provider_model_id,
                "messages": _chat_messages(request_body.get("input")),
                "stream": True,
                "stream_options": {"include_usage": True},
            }
        )
        return self._open_stream(
            "/v1/chat/completions",
            payload,
            candidate,
            self._stream_events,
        )

    def _open_stream(
        self,
        path: str,
        payload: dict[str, Any],
        candidate: ProviderModel,
        event_factory: Callable[[Iterator[str], ProviderModel], Iterator[ProviderStreamEvent]],
    ) -> ProviderStream:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return _open_provider_stream(
            url=self._url(path),
            headers=headers,
            payload=payload,
            timeout_seconds=self.timeout_seconds,
            candidate=candidate,
            event_factory=event_factory,
        )

    @staticmethod
    def _stream_events(
        lines: Iterator[str],
        candidate: ProviderModel,
    ) -> Iterator[ProviderStreamEvent]:
        input_tokens: int | None = None
        output_tokens: int | None = None
        try:
            for _event_name, raw_data in _sse_messages(lines):
                if raw_data == "[DONE]":
                    yield ProviderStreamEvent(
                        completed=True,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                    return
                data = json.loads(raw_data)
                if not isinstance(data, dict):
                    raise TypeError("OpenAI stream frame must be an object")
                usage = data.get("usage")
                if isinstance(usage, dict):
                    input_tokens = int(usage.get("prompt_tokens", 0))
                    output_tokens = int(usage.get("completion_tokens", 0))
                choices = data.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                delta = choices[0].get("delta")
                if not isinstance(delta, dict):
                    continue
                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield ProviderStreamEvent(delta=content)
        except ProviderDispatchError:
            raise
        except httpx.TimeoutException as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "upstream_timeout",
            ) from error
        except httpx.HTTPError as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "upstream_network_error",
            ) from error
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            ) from error
        raise ProviderDispatchError(
            candidate.provider,
            candidate.provider_model_id,
            "invalid_upstream_response",
        )

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        candidate: ProviderModel,
    ) -> dict[str, Any]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"

        try:
            response = httpx.post(
                self._url(path),
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "upstream_timeout",
            ) from error
        except httpx.HTTPError as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "upstream_network_error",
            ) from error

        if not response.is_success:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                f"upstream_http_{response.status_code}",
            )

        try:
            data = response.json()
        except ValueError as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            ) from error
        if not isinstance(data, dict):
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            )
        return data

    def _url(self, path: str) -> str:
        if self.base_url.endswith("/v1") and path.startswith("/v1/"):
            return f"{self.base_url}{path.removeprefix('/v1')}"
        return f"{self.base_url}{path}"


class AnthropicProviderAdapter:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        api_version: str = "2023-06-01",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_version = api_version
        self.timeout_seconds = timeout_seconds

    def dispatch_response(self, request_body: dict[str, Any], candidate: ProviderModel) -> AdapterResponse:
        payload = _provider_options(request_body)
        payload.update(_anthropic_portable_options(request_body))
        payload.update(
            {
                "model": candidate.provider_model_id,
                "messages": _anthropic_messages(request_body.get("input")),
            }
        )
        max_output_tokens = request_body.get("max_output_tokens")
        if isinstance(max_output_tokens, int) and max_output_tokens > 0:
            payload["max_tokens"] = max_output_tokens
        else:
            payload.setdefault("max_tokens", 1024)
        system = _anthropic_system(request_body.get("input"))
        if system:
            payload["system"] = system
        data = self._post_json("/v1/messages", payload, candidate)

        try:
            output_text = _anthropic_output_text(data["content"])
            tool_calls = _anthropic_tool_calls(data["content"])
            usage = data["usage"]
            input_tokens = int(usage["input_tokens"])
            output_tokens = int(usage["output_tokens"])
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            ) from error

        return AdapterResponse(
            output_text=output_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
        )

    def start_response_stream(
        self,
        request_body: dict[str, Any],
        candidate: ProviderModel,
    ) -> ProviderStream:
        payload = _provider_options(request_body)
        payload.update(_anthropic_portable_options(request_body))
        payload.update(
            {
                "model": candidate.provider_model_id,
                "messages": _anthropic_messages(request_body.get("input")),
                "stream": True,
            }
        )
        max_output_tokens = request_body.get("max_output_tokens")
        if isinstance(max_output_tokens, int) and max_output_tokens > 0:
            payload["max_tokens"] = max_output_tokens
        else:
            payload.setdefault("max_tokens", 1024)
        system = _anthropic_system(request_body.get("input"))
        if system:
            payload["system"] = system

        headers = {
            "content-type": "application/json",
            "anthropic-version": self.api_version,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return _open_provider_stream(
            url=self._url("/v1/messages"),
            headers=headers,
            payload=payload,
            timeout_seconds=self.timeout_seconds,
            candidate=candidate,
            event_factory=self._stream_events,
        )

    @staticmethod
    def _stream_events(
        lines: Iterator[str],
        candidate: ProviderModel,
    ) -> Iterator[ProviderStreamEvent]:
        input_tokens: int | None = None
        output_tokens: int | None = None
        try:
            for event_name, raw_data in _sse_messages(lines):
                data = json.loads(raw_data)
                if not isinstance(data, dict):
                    raise TypeError("Anthropic stream frame must be an object")
                if event_name == "message_start":
                    message = data.get("message")
                    usage = message.get("usage") if isinstance(message, dict) else None
                    if isinstance(usage, dict):
                        input_tokens = int(usage.get("input_tokens", 0))
                elif event_name == "content_block_delta":
                    delta = data.get("delta")
                    if isinstance(delta, dict) and delta.get("type") == "text_delta":
                        text = delta.get("text")
                        if isinstance(text, str) and text:
                            yield ProviderStreamEvent(delta=text)
                elif event_name == "message_delta":
                    usage = data.get("usage")
                    if isinstance(usage, dict):
                        output_tokens = int(usage.get("output_tokens", 0))
                elif event_name == "message_stop":
                    yield ProviderStreamEvent(
                        completed=True,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                    return
                elif event_name == "error":
                    raise TypeError("Anthropic stream returned an error event")
        except httpx.TimeoutException as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "upstream_timeout",
            ) from error
        except httpx.HTTPError as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "upstream_network_error",
            ) from error
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            ) from error
        raise ProviderDispatchError(
            candidate.provider,
            candidate.provider_model_id,
            "invalid_upstream_response",
        )

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        candidate: ProviderModel,
    ) -> dict[str, Any]:
        headers = {
            "content-type": "application/json",
            "anthropic-version": self.api_version,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key

        try:
            response = httpx.post(
                self._url(path),
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "upstream_timeout",
            ) from error
        except httpx.HTTPError as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "upstream_network_error",
            ) from error

        if not response.is_success:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                f"upstream_http_{response.status_code}",
            )

        try:
            data = response.json()
        except ValueError as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            ) from error
        if not isinstance(data, dict):
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            )
        return data

    def _url(self, path: str) -> str:
        if self.base_url.endswith("/v1") and path.startswith("/v1/"):
            return f"{self.base_url}{path.removeprefix('/v1')}"
        return f"{self.base_url}{path}"


class GeminiProviderAdapter:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def dispatch_response(self, request_body: dict[str, Any], candidate: ProviderModel) -> AdapterResponse:
        payload = _provider_options(request_body)
        payload.update(_gemini_portable_options(request_body))
        payload["contents"] = _gemini_contents(request_body.get("input"))
        system_instruction = _gemini_system_instruction(request_body.get("input"))
        if system_instruction:
            payload["systemInstruction"] = system_instruction
        generation_config = _gemini_generation_config(request_body, payload.get("generationConfig"))
        if generation_config:
            payload["generationConfig"] = generation_config
        model = quote(candidate.provider_model_id, safe="")
        data = self._post_json(f"/v1beta/models/{model}:generateContent", payload, candidate)

        try:
            parts = data["candidates"][0]["content"]["parts"]
            output_text = _gemini_output_text(parts)
            tool_calls = _gemini_tool_calls(parts)
            if not output_text and not tool_calls:
                raise TypeError("Gemini candidate has no supported output")
            usage = data["usageMetadata"]
            input_tokens = int(usage["promptTokenCount"])
            output_tokens = int(usage["candidatesTokenCount"])
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            ) from error

        return AdapterResponse(
            output_text=output_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
        )

    def start_response_stream(
        self,
        request_body: dict[str, Any],
        candidate: ProviderModel,
    ) -> ProviderStream:
        payload = _provider_options(request_body)
        payload.update(_gemini_portable_options(request_body))
        payload["contents"] = _gemini_contents(request_body.get("input"))
        system_instruction = _gemini_system_instruction(request_body.get("input"))
        if system_instruction:
            payload["systemInstruction"] = system_instruction
        generation_config = _gemini_generation_config(request_body, payload.get("generationConfig"))
        if generation_config:
            payload["generationConfig"] = generation_config
        model = quote(candidate.provider_model_id, safe="")
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        return _open_provider_stream(
            url=self._url(f"/v1beta/models/{model}:streamGenerateContent?alt=sse"),
            headers=headers,
            payload=payload,
            timeout_seconds=self.timeout_seconds,
            candidate=candidate,
            event_factory=self._stream_events,
        )

    @staticmethod
    def _stream_events(
        lines: Iterator[str],
        candidate: ProviderModel,
    ) -> Iterator[ProviderStreamEvent]:
        input_tokens: int | None = None
        output_tokens: int | None = None
        saw_frame = False
        try:
            for _event_name, raw_data in _sse_messages(lines):
                data = json.loads(raw_data)
                if not isinstance(data, dict):
                    raise TypeError("Gemini stream frame must be an object")
                saw_frame = True
                usage = data.get("usageMetadata")
                if isinstance(usage, dict):
                    input_tokens = int(usage.get("promptTokenCount", 0))
                    output_tokens = int(usage.get("candidatesTokenCount", 0))
                candidates = data.get("candidates")
                if not isinstance(candidates, list) or not candidates:
                    continue
                content = candidates[0].get("content")
                parts = content.get("parts") if isinstance(content, dict) else None
                if not isinstance(parts, list):
                    continue
                for part in parts:
                    text = part.get("text") if isinstance(part, dict) else None
                    if isinstance(text, str) and text:
                        yield ProviderStreamEvent(delta=text)
        except httpx.TimeoutException as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "upstream_timeout",
            ) from error
        except httpx.HTTPError as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "upstream_network_error",
            ) from error
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            ) from error
        if saw_frame:
            yield ProviderStreamEvent(
                completed=True,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            return
        raise ProviderDispatchError(
            candidate.provider,
            candidate.provider_model_id,
            "invalid_upstream_response",
        )

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        candidate: ProviderModel,
    ) -> dict[str, Any]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key

        try:
            response = httpx.post(
                self._url(path),
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "upstream_timeout",
            ) from error
        except httpx.HTTPError as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "upstream_network_error",
            ) from error

        if not response.is_success:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                f"upstream_http_{response.status_code}",
            )

        try:
            data = response.json()
        except ValueError as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            ) from error
        if not isinstance(data, dict):
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            )
        return data

    def _url(self, path: str) -> str:
        if self.base_url.endswith("/v1beta") and path.startswith("/v1beta/"):
            return f"{self.base_url}{path.removeprefix('/v1beta')}"
        return f"{self.base_url}{path}"


class OllamaProviderAdapter:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def dispatch_response(self, request_body: dict[str, Any], candidate: ProviderModel) -> AdapterResponse:
        payload = _provider_options(request_body)
        payload.update(_ollama_portable_options(request_body, payload.get("options")))
        payload.update(
            {
                "model": candidate.provider_model_id,
                "messages": _chat_messages(request_body.get("input")),
                "stream": False,
            }
        )
        data = self._post_json("/api/chat", payload, candidate)

        try:
            message = data["message"]
            if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                raise TypeError("Ollama chat message is malformed")
            output_text = message["content"]
            if not output_text:
                raise TypeError("Ollama chat message is empty")
            input_tokens = int(data["prompt_eval_count"])
            output_tokens = int(data["eval_count"])
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            ) from error

        return AdapterResponse(
            output_text=output_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def dispatch_embedding(self, request_body: dict[str, Any], candidate: ProviderModel) -> AdapterResponse:
        payload = _provider_options(request_body)
        payload.update(
            {
                "model": candidate.provider_model_id,
                "input": request_body.get("input"),
            }
        )
        data = self._post_json("/api/embed", payload, candidate)

        try:
            embedding = [float(value) for value in data["embeddings"][0]]
            if not embedding:
                raise TypeError("Ollama embedding is empty")
            input_tokens = int(data["prompt_eval_count"])
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            ) from error

        return AdapterResponse(
            output_text="",
            input_tokens=input_tokens,
            output_tokens=0,
            embedding=embedding,
        )

    def start_response_stream(
        self,
        request_body: dict[str, Any],
        candidate: ProviderModel,
    ) -> ProviderStream:
        payload = _provider_options(request_body)
        payload.update(_ollama_portable_options(request_body, payload.get("options")))
        payload.update(
            {
                "model": candidate.provider_model_id,
                "messages": _chat_messages(request_body.get("input")),
                "stream": True,
            }
        )
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return _open_provider_stream(
            url=self._url("/api/chat"),
            headers=headers,
            payload=payload,
            timeout_seconds=self.timeout_seconds,
            candidate=candidate,
            event_factory=self._stream_events,
        )

    @staticmethod
    def _stream_events(
        lines: Iterator[str],
        candidate: ProviderModel,
    ) -> Iterator[ProviderStreamEvent]:
        try:
            for line in lines:
                if not line:
                    continue
                data = json.loads(line)
                if not isinstance(data, dict):
                    raise TypeError("Ollama stream frame must be an object")
                if data.get("error"):
                    raise TypeError("Ollama stream returned an error")
                message = data.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str) and content:
                    yield ProviderStreamEvent(delta=content)
                if data.get("done") is True:
                    yield ProviderStreamEvent(
                        completed=True,
                        input_tokens=int(data.get("prompt_eval_count", 0)),
                        output_tokens=int(data.get("eval_count", 0)),
                    )
                    return
        except httpx.TimeoutException as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "upstream_timeout",
            ) from error
        except httpx.HTTPError as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "upstream_network_error",
            ) from error
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            ) from error
        raise ProviderDispatchError(
            candidate.provider,
            candidate.provider_model_id,
            "invalid_upstream_response",
        )

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        candidate: ProviderModel,
    ) -> dict[str, Any]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"

        try:
            response = httpx.post(
                self._url(path),
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "upstream_timeout",
            ) from error
        except httpx.HTTPError as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "upstream_network_error",
            ) from error

        if not response.is_success:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                f"upstream_http_{response.status_code}",
            )

        try:
            data = response.json()
        except ValueError as error:
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            ) from error
        if not isinstance(data, dict):
            raise ProviderDispatchError(
                candidate.provider,
                candidate.provider_model_id,
                "invalid_upstream_response",
            )
        return data

    def _url(self, path: str) -> str:
        if self.base_url.endswith("/api") and path.startswith("/api/"):
            return f"{self.base_url}{path.removeprefix('/api')}"
        return f"{self.base_url}{path}"


class CompositeProviderAdapter:
    def __init__(self, default_adapter: Any, provider_adapters: dict[str, Any]) -> None:
        self.default_adapter = default_adapter
        self.provider_adapters = provider_adapters

    def dispatch_response(self, request_body: dict[str, Any], candidate: ProviderModel) -> AdapterResponse:
        return self._adapter(candidate).dispatch_response(request_body, candidate)

    def dispatch_embedding(self, request_body: dict[str, Any], candidate: ProviderModel) -> AdapterResponse:
        return self._adapter(candidate).dispatch_embedding(request_body, candidate)

    def start_response_stream(
        self,
        request_body: dict[str, Any],
        candidate: ProviderModel,
    ) -> ProviderStream:
        return self._adapter(candidate).start_response_stream(request_body, candidate)

    def _adapter(self, candidate: ProviderModel) -> Any:
        return self.provider_adapters.get(candidate.provider, self.default_adapter)


def _open_provider_stream(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
    candidate: ProviderModel,
    event_factory: Callable[[Iterator[str], ProviderModel], Iterator[ProviderStreamEvent]],
) -> ProviderStream:
    context = httpx.stream(
        "POST",
        url,
        headers=headers,
        json=payload,
        timeout=timeout_seconds,
    )
    try:
        response = context.__enter__()
    except httpx.TimeoutException as error:
        raise ProviderDispatchError(
            candidate.provider,
            candidate.provider_model_id,
            "upstream_timeout",
        ) from error
    except httpx.HTTPError as error:
        raise ProviderDispatchError(
            candidate.provider,
            candidate.provider_model_id,
            "upstream_network_error",
        ) from error

    if not response.is_success:
        context.__exit__(None, None, None)
        raise ProviderDispatchError(
            candidate.provider,
            candidate.provider_model_id,
            f"upstream_http_{response.status_code}",
        )

    return ProviderStream(
        events=event_factory(response.iter_lines(), candidate),
        close=lambda: context.__exit__(None, None, None),
        candidate=candidate,
    )


def _sse_messages(lines: Iterator[str]) -> Iterator[tuple[str, str]]:
    event_name = ""
    data_lines: list[str] = []
    for line in lines:
        if not line:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = ""
            data_lines = []
            continue
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").lstrip())
    if data_lines:
        yield event_name, "\n".join(data_lines)


def _text_deltas(text: str) -> Iterator[str]:
    words = text.split(" ")
    for index, word in enumerate(words):
        suffix = " " if index < len(words) - 1 else ""
        if word or suffix:
            yield f"{word}{suffix}"


def extract_text(raw_input: Any) -> str:
    if isinstance(raw_input, str):
        return raw_input
    if not isinstance(raw_input, list):
        return ""

    chunks: list[str] = []
    for message in raw_input:
        if not isinstance(message, dict):
            continue
        content = message.get("content", [])
        if isinstance(content, str):
            chunks.append(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"input_text", "text"}:
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return " ".join(chunks)


def _requests_json_schema(request_body: dict[str, Any]) -> bool:
    response = request_body.get("response")
    if not isinstance(response, dict):
        return False
    response_format = response.get("format")
    return isinstance(response_format, dict) and response_format.get("type") == "json_schema"


def embedding_text(raw_input: Any) -> str:
    if isinstance(raw_input, str):
        return raw_input
    if isinstance(raw_input, list):
        return " ".join(str(item) for item in raw_input)
    return str(raw_input)


def count_tokens(text: str) -> int:
    return len([part for part in text.split() if part]) or (1 if text else 0)


def deterministic_embedding(text: str, model: str) -> list[float]:
    digest = hashlib.sha256(f"{model}:{text}".encode("utf-8")).digest()
    return [round(byte / 255, 6) for byte in digest[:8]]


def _chat_messages(raw_input: Any) -> list[dict[str, Any]]:
    if isinstance(raw_input, str):
        return [{"role": "user", "content": raw_input}]
    if not isinstance(raw_input, list):
        return []

    messages: list[dict[str, Any]] = []
    for raw_message in raw_input:
        if not isinstance(raw_message, dict):
            continue
        role = raw_message.get("role", "user")
        raw_content = raw_message.get("content", "")
        messages.append({"role": role, "content": _chat_content(raw_content)})
    return messages


def _anthropic_messages(raw_input: Any) -> list[dict[str, Any]]:
    if isinstance(raw_input, str):
        return [{"role": "user", "content": raw_input}]
    if not isinstance(raw_input, list):
        return []

    messages: list[dict[str, Any]] = []
    for raw_message in raw_input:
        if not isinstance(raw_message, dict):
            continue
        role = raw_message.get("role", "user")
        if role == "system":
            continue
        messages.append(
            {
                "role": role,
                "content": _anthropic_content(raw_message.get("content", "")),
            }
        )
    return messages


def _gemini_contents(raw_input: Any) -> list[dict[str, Any]]:
    if isinstance(raw_input, str):
        return [{"role": "user", "parts": [{"text": raw_input}]}]
    if not isinstance(raw_input, list):
        return []

    contents: list[dict[str, Any]] = []
    for raw_message in raw_input:
        if not isinstance(raw_message, dict) or raw_message.get("role") == "system":
            continue
        role = "model" if raw_message.get("role") == "assistant" else "user"
        parts = _gemini_parts(raw_message.get("content", ""))
        contents.append({"role": role, "parts": parts})
    return contents


def _gemini_parts(raw_content: Any) -> list[dict[str, Any]]:
    if isinstance(raw_content, str):
        return [{"text": raw_content}]
    if not isinstance(raw_content, list):
        return []

    parts: list[dict[str, Any]] = []
    for part in raw_content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in {"input_text", "text"} and isinstance(part.get("text"), str):
            parts.append({"text": part["text"]})
    return parts


def _gemini_system_instruction(raw_input: Any) -> dict[str, Any] | None:
    if not isinstance(raw_input, list):
        return None
    parts: list[dict[str, str]] = []
    for raw_message in raw_input:
        if not isinstance(raw_message, dict) or raw_message.get("role") != "system":
            continue
        for part in _gemini_parts(raw_message.get("content", "")):
            parts.append({"text": part["text"]})
    return {"parts": parts} if parts else None


def _gemini_generation_config(
    request_body: dict[str, Any],
    native_config: Any,
) -> dict[str, Any]:
    config = dict(native_config) if isinstance(native_config, dict) else {}

    max_output_tokens = request_body.get("max_output_tokens")
    if isinstance(max_output_tokens, int) and not isinstance(max_output_tokens, bool):
        config["maxOutputTokens"] = max_output_tokens

    response = request_body.get("response")
    response_format = response.get("format") if isinstance(response, dict) else None
    if isinstance(response_format, dict):
        if response_format.get("type") == "json_object":
            config["responseMimeType"] = "application/json"
        elif response_format.get("type") == "json_schema" and isinstance(
            response_format.get("json_schema"), dict
        ):
            config["responseMimeType"] = "application/json"
            config["responseJsonSchema"] = response_format["json_schema"]

    return config


def _ollama_portable_options(
    request_body: dict[str, Any],
    native_options: Any,
) -> dict[str, Any]:
    portable: dict[str, Any] = {}
    options = dict(native_options) if isinstance(native_options, dict) else {}

    max_output_tokens = request_body.get("max_output_tokens")
    if isinstance(max_output_tokens, int) and not isinstance(max_output_tokens, bool):
        options["num_predict"] = max_output_tokens
    if options:
        portable["options"] = options

    response = request_body.get("response")
    response_format = response.get("format") if isinstance(response, dict) else None
    if isinstance(response_format, dict) and response_format.get("type") == "json_object":
        portable["format"] = "json"

    return portable


def _anthropic_system(raw_input: Any) -> str:
    if not isinstance(raw_input, list):
        return ""
    chunks: list[str] = []
    for raw_message in raw_input:
        if not isinstance(raw_message, dict) or raw_message.get("role") != "system":
            continue
        content = raw_message.get("content", "")
        if isinstance(content, str):
            chunks.append(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"input_text", "text"}:
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "\n".join(chunks)


def _anthropic_content(raw_content: Any) -> Any:
    if isinstance(raw_content, str):
        return raw_content
    if not isinstance(raw_content, list):
        return ""

    content: list[dict[str, Any]] = []
    for part in raw_content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in {"input_text", "text"} and isinstance(part.get("text"), str):
            content.append({"type": "text", "text": part["text"]})
        elif part_type in {"input_image", "image"} and isinstance(part.get("image_url"), str):
            content.append(
                {
                    "type": "image",
                    "source": {"type": "url", "url": part["image_url"]},
                }
            )
    return content


def _anthropic_output_text(raw_content: Any) -> str:
    if not isinstance(raw_content, list):
        raise TypeError("Anthropic content must be a list")
    chunks: list[str] = []
    for block in raw_content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            raise TypeError("Anthropic text block is malformed")
        chunks.append(text)
    return "".join(chunks)


def _gemini_output_text(raw_parts: Any) -> str:
    if not isinstance(raw_parts, list):
        raise TypeError("Gemini parts must be a list")
    chunks: list[str] = []
    for part in raw_parts:
        if not isinstance(part, dict) or "text" not in part:
            continue
        text = part["text"]
        if not isinstance(text, str):
            raise TypeError("Gemini text part is malformed")
        chunks.append(text)
    return "".join(chunks)


def _gemini_tool_calls(raw_parts: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw_parts, list):
        raise TypeError("Gemini parts must be a list")

    tool_calls: list[dict[str, Any]] = []
    for index, part in enumerate(raw_parts):
        if not isinstance(part, dict) or "functionCall" not in part:
            continue
        function_call = part["functionCall"]
        if not isinstance(function_call, dict):
            raise TypeError("Gemini function call is malformed")
        name = function_call.get("name")
        arguments = function_call.get("args")
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise TypeError("Gemini function call fields are malformed")
        tool_calls.append(
            {
                "type": "function_call",
                "id": f"call_gemini_{index}",
                "name": name,
                "arguments": json.dumps(arguments, separators=(",", ":"), ensure_ascii=True),
            }
        )
    return tuple(tool_calls)


def _gemini_portable_options(request_body: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    tools = _gemini_tools(request_body.get("tools"))
    if tools:
        options["tools"] = [{"functionDeclarations": tools}]
    tool_config = _gemini_tool_config(request_body.get("tool_choice"))
    if tool_config is not None:
        options["toolConfig"] = {"functionCallingConfig": tool_config}
    return options


def _gemini_tools(raw_tools: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_tools, list):
        return []

    tools: list[dict[str, Any]] = []
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, dict) or raw_tool.get("type") != "function":
            continue
        tool: dict[str, Any] = {
            "name": raw_tool.get("name"),
            "parameters": raw_tool.get("parameters", {}),
        }
        if isinstance(raw_tool.get("description"), str):
            tool["description"] = raw_tool["description"]
        tools.append(tool)
    return tools


def _gemini_tool_config(raw_tool_choice: Any) -> dict[str, Any] | None:
    if raw_tool_choice is None:
        return None
    if isinstance(raw_tool_choice, str):
        if raw_tool_choice == "auto":
            return {"mode": "AUTO"}
        if raw_tool_choice == "none":
            return {"mode": "NONE"}
        if raw_tool_choice == "required":
            return {"mode": "ANY"}
        return {"mode": "ANY", "allowedFunctionNames": [raw_tool_choice]}
    if isinstance(raw_tool_choice, dict):
        function = raw_tool_choice.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return {"mode": "ANY", "allowedFunctionNames": [function["name"]]}
        if raw_tool_choice.get("type") == "function" and isinstance(raw_tool_choice.get("name"), str):
            return {"mode": "ANY", "allowedFunctionNames": [raw_tool_choice["name"]]}
    return None


def _anthropic_tool_calls(raw_content: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw_content, list):
        raise TypeError("Anthropic content must be a list")

    tool_calls: list[dict[str, Any]] = []
    for block in raw_content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        call_id = block.get("id")
        name = block.get("name")
        arguments = block.get("input")
        if not isinstance(call_id, str) or not isinstance(name, str) or not isinstance(arguments, dict):
            raise TypeError("Anthropic tool use block is malformed")
        tool_calls.append(
            {
                "type": "function_call",
                "id": call_id,
                "name": name,
                "arguments": json.dumps(arguments, separators=(",", ":"), ensure_ascii=True),
            }
        )
    return tuple(tool_calls)


def _anthropic_portable_options(request_body: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    tools = _anthropic_tools(request_body.get("tools"))
    if tools:
        options["tools"] = tools
    tool_choice = _anthropic_tool_choice(request_body.get("tool_choice"))
    if tool_choice is not None:
        options["tool_choice"] = tool_choice
    return options


def _anthropic_tools(raw_tools: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_tools, list):
        return []

    tools: list[dict[str, Any]] = []
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, dict) or raw_tool.get("type") != "function":
            continue
        tool: dict[str, Any] = {
            "name": raw_tool.get("name"),
            "input_schema": raw_tool.get("parameters", {}),
        }
        if isinstance(raw_tool.get("description"), str):
            tool["description"] = raw_tool["description"]
        if isinstance(raw_tool.get("strict"), bool):
            tool["strict"] = raw_tool["strict"]
        tools.append(tool)
    return tools


def _anthropic_tool_choice(raw_tool_choice: Any) -> dict[str, Any] | None:
    if raw_tool_choice is None:
        return None
    if isinstance(raw_tool_choice, str):
        if raw_tool_choice == "required":
            return {"type": "any"}
        if raw_tool_choice in {"auto", "none"}:
            return {"type": raw_tool_choice}
        return {"type": "tool", "name": raw_tool_choice}
    if isinstance(raw_tool_choice, dict):
        function = raw_tool_choice.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return {"type": "tool", "name": function["name"]}
        if raw_tool_choice.get("type") == "function" and isinstance(raw_tool_choice.get("name"), str):
            return {"type": "tool", "name": raw_tool_choice["name"]}
    return None


def _chat_content(raw_content: Any) -> Any:
    if isinstance(raw_content, str):
        return raw_content
    if not isinstance(raw_content, list):
        return ""

    parts: list[dict[str, Any]] = []
    for part in raw_content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in {"input_text", "text"} and isinstance(part.get("text"), str):
            parts.append({"type": "text", "text": part["text"]})
        elif part_type in {"input_image", "image"} and isinstance(part.get("image_url"), str):
            parts.append({"type": "image_url", "image_url": {"url": part["image_url"]}})

    if parts and all(part["type"] == "text" for part in parts):
        return " ".join(part["text"] for part in parts)
    return parts


def _chat_output_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
        )
    raise TypeError("unsupported chat content")


def _chat_tool_calls(message: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    raw_tool_calls = message.get("tool_calls", [])
    if not isinstance(raw_tool_calls, list):
        raise TypeError("chat tool calls must be a list")

    tool_calls: list[dict[str, Any]] = []
    for raw_tool_call in raw_tool_calls:
        if not isinstance(raw_tool_call, dict) or raw_tool_call.get("type") != "function":
            raise TypeError("unsupported chat tool call")
        function = raw_tool_call.get("function")
        if not isinstance(function, dict):
            raise TypeError("chat function call must be an object")
        call_id = raw_tool_call.get("id")
        name = function.get("name")
        arguments = function.get("arguments")
        if not all(isinstance(value, str) for value in (call_id, name, arguments)):
            raise TypeError("chat function call fields must be strings")
        tool_calls.append(
            {
                "type": "function_call",
                "id": call_id,
                "name": name,
                "arguments": arguments,
            }
        )
    return tuple(tool_calls)


def _provider_options(request_body: dict[str, Any]) -> dict[str, Any]:
    native = request_body.get("native")
    if not isinstance(native, dict):
        return {}
    options = native.get("provider_options")
    return dict(options) if isinstance(options, dict) else {}


def _openai_portable_options(request_body: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}

    tools = _openai_tools(request_body.get("tools"))
    if tools:
        options["tools"] = tools

    tool_choice = _openai_tool_choice(request_body.get("tool_choice"))
    if tool_choice is not None:
        options["tool_choice"] = tool_choice

    response_format = _openai_response_format(request_body.get("response"))
    if response_format is not None:
        options["response_format"] = response_format

    return options


def _openai_tools(raw_tools: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_tools, list):
        return []

    tools: list[dict[str, Any]] = []
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, dict) or raw_tool.get("type") != "function":
            continue
        function: dict[str, Any] = {
            "name": raw_tool.get("name"),
            "parameters": raw_tool.get("parameters", {}),
        }
        if isinstance(raw_tool.get("description"), str):
            function["description"] = raw_tool["description"]
        if isinstance(raw_tool.get("strict"), bool):
            function["strict"] = raw_tool["strict"]
        tools.append({"type": "function", "function": function})
    return tools


def _openai_tool_choice(raw_tool_choice: Any) -> Any:
    if raw_tool_choice is None:
        return None
    if isinstance(raw_tool_choice, str):
        if raw_tool_choice in {"auto", "none", "required"}:
            return raw_tool_choice
        return {"type": "function", "function": {"name": raw_tool_choice}}
    if isinstance(raw_tool_choice, dict):
        if isinstance(raw_tool_choice.get("function"), dict):
            return dict(raw_tool_choice)
        if raw_tool_choice.get("type") == "function" and isinstance(raw_tool_choice.get("name"), str):
            return {
                "type": "function",
                "function": {"name": raw_tool_choice["name"]},
            }
    return None


def _openai_response_format(raw_response: Any) -> dict[str, Any] | None:
    if not isinstance(raw_response, dict):
        return None
    raw_format = raw_response.get("format")
    if not isinstance(raw_format, dict):
        return None

    format_type = raw_format.get("type")
    if format_type == "json_object":
        return {"type": "json_object"}
    if format_type != "json_schema" or not isinstance(raw_format.get("json_schema"), dict):
        return None

    return {
        "type": "json_schema",
        "json_schema": {
            "name": raw_format.get("name", "mixapi_response"),
            "schema": raw_format["json_schema"],
            "strict": raw_format.get("strict", True),
        },
    }
