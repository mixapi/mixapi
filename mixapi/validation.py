from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from mixapi.errors import capability_unsupported, validation_error
from mixapi.structured_output import InvalidResponseSchema, check_response_schema


def validate_response_request(request_body: dict[str, Any], idempotency_key: str | None) -> None:
    stream = request_body.get("stream", False)
    if not isinstance(stream, bool):
        raise validation_error("invalid_stream", "Request field `stream` must be a boolean.")
    _validate_response_schema(request_body, stream)
    if stream and idempotency_key:
        raise validation_error(
            "streaming_idempotency_unsupported",
            "Idempotency keys are not supported for streaming responses.",
        )
    if not _has_response_input(request_body.get("input")):
        raise validation_error("input_required", "Request field `input` must contain content.")
    _validate_max_output_tokens(request_body.get("max_output_tokens"))
    tools = request_body.get("tools")
    _validate_tools(tools)
    if stream and isinstance(tools, list) and tools:
        raise capability_unsupported(
            "streaming_tools_unsupported",
            "Streaming function tool calls are not supported.",
        )
    _validate_routing_budget(request_body.get("routing"))


def _validate_response_schema(request_body: dict[str, Any], stream: bool) -> None:
    response = request_body.get("response")
    if not isinstance(response, dict):
        return
    response_format = response.get("format")
    if not isinstance(response_format, dict) or response_format.get("type") != "json_schema":
        return

    try:
        check_response_schema(response_format.get("json_schema"))
    except InvalidResponseSchema as error:
        raise validation_error(
            "invalid_json_schema",
            "Response JSON Schema must be a valid Draft 2020-12 schema object.",
        ) from error
    if stream:
        raise capability_unsupported(
            "streaming_structured_output_unsupported",
            "Streaming is not supported for validated structured output.",
        )


def validate_embedding_request(request_body: dict[str, Any]) -> None:
    _validate_routing_budget(request_body.get("routing"))
    raw_input = request_body.get("input")
    if isinstance(raw_input, str):
        if raw_input.strip():
            return
        raise validation_error("input_required", "Request field `input` must contain text.")

    if isinstance(raw_input, list):
        if raw_input and all(isinstance(item, str) and item.strip() for item in raw_input):
            return
        raise validation_error("input_required", "Request field `input` must contain text.")

    raise validation_error("input_required", "Request field `input` must contain text.")


def _has_response_input(raw_input: Any) -> bool:
    if isinstance(raw_input, str):
        return bool(raw_input.strip())
    if not isinstance(raw_input, list) or not raw_input:
        return False

    for message in raw_input:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return True
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in {"input_text", "text"} and isinstance(part.get("text"), str):
                if part["text"].strip():
                    return True
            if part_type in {"input_image", "image"} and part.get("image_url"):
                return True
            if part_type in {"input_file", "file"} and (part.get("file_ref") or part.get("file_url")):
                return True
    return False


def _validate_tools(raw_tools: Any) -> None:
    if raw_tools is None:
        return
    if not isinstance(raw_tools, list):
        raise validation_error("invalid_tools", "Request field `tools` must be a list.")

    for tool in raw_tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise validation_error("invalid_tools", "Only function tools are supported.")
        if not isinstance(tool.get("name"), str) or not tool["name"].strip():
            raise validation_error("invalid_tools", "Function tools require a non-empty `name`.")
        if not isinstance(tool.get("parameters"), dict):
            raise validation_error("invalid_tools", "Function tools require object `parameters`.")
        if "description" in tool and not isinstance(tool["description"], str):
            raise validation_error("invalid_tools", "Function tool `description` must be a string.")
        if "strict" in tool and not isinstance(tool["strict"], bool):
            raise validation_error("invalid_tools", "Function tool `strict` must be a boolean.")


def _validate_max_output_tokens(value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise validation_error(
            "invalid_max_output_tokens",
            "Request field `max_output_tokens` must be a positive integer.",
        )


def _validate_routing_budget(routing: Any) -> None:
    if routing is None or not isinstance(routing, dict) or "max_cost_usd" not in routing:
        return

    value = routing["max_cost_usd"]
    if isinstance(value, bool):
        raise validation_error(
            "invalid_max_cost_usd",
            "Routing field `max_cost_usd` must be a positive decimal.",
        )
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        amount = Decimal("0")
    if not amount.is_finite() or amount <= 0:
        raise validation_error(
            "invalid_max_cost_usd",
            "Routing field `max_cost_usd` must be a positive decimal.",
        )
