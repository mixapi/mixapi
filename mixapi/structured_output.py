from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


MAX_VALIDATION_FAILURES = 3
MAX_FAILURE_PATH_CHARS = 140
MAX_FAILURE_MESSAGE_CHARS = 64
CORRECTIVE_RETRY_TOKEN_RESERVE = 320


class InvalidResponseSchema(ValueError):
    pass


@dataclass(frozen=True)
class ValidationFailure:
    path: str
    message: str


@dataclass(frozen=True)
class StructuredOutputValidation:
    valid: bool
    value: Any | None = None
    failures: tuple[ValidationFailure, ...] = ()


def response_schema(request_body: dict[str, Any]) -> dict[str, Any] | None:
    response = request_body.get("response")
    if not isinstance(response, dict):
        return None
    response_format = response.get("format")
    if not isinstance(response_format, dict) or response_format.get("type") != "json_schema":
        return None
    schema = response_format.get("json_schema")
    return schema if isinstance(schema, dict) else None


def check_response_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise InvalidResponseSchema("JSON Schema must be an object.")
    if _contains_remote_reference(schema):
        raise InvalidResponseSchema("Remote JSON Schema references are not supported.")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise InvalidResponseSchema("JSON Schema is not valid Draft 2020-12.") from error
    return schema


def validate_output(output_text: str, schema: dict[str, Any]) -> StructuredOutputValidation:
    try:
        value = json.loads(output_text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, TypeError, ValueError):
        return StructuredOutputValidation(
            valid=False,
            failures=(ValidationFailure("$", "invalid JSON"),),
        )

    validator = Draft202012Validator(schema)
    errors = sorted(
        validator.iter_errors(value),
        key=lambda error: (_failure_path(error.absolute_path), error.validator or ""),
    )
    if not errors:
        return StructuredOutputValidation(valid=True, value=value)

    failures = sorted(
        (
            ValidationFailure(
                path=_bounded_text(
                    _failure_path(error.absolute_path, error.validator, error.message),
                    MAX_FAILURE_PATH_CHARS,
                ),
                message=_bounded_text(
                    _failure_message(error.validator, error.validator_value),
                    MAX_FAILURE_MESSAGE_CHARS,
                ),
            )
            for error in errors
        ),
        key=lambda failure: (failure.path, failure.message),
    )
    return StructuredOutputValidation(
        valid=False,
        failures=tuple(failures[:MAX_VALIDATION_FAILURES]),
    )


def corrective_request(
    request_body: dict[str, Any],
    failures: tuple[ValidationFailure, ...],
) -> dict[str, Any]:
    corrected = deepcopy(request_body)
    raw_input = corrected.get("input")
    if isinstance(raw_input, str):
        corrected["input"] = [{"role": "user", "content": raw_input}]
    elif isinstance(raw_input, list):
        corrected["input"] = raw_input
    else:
        corrected["input"] = []

    bounded_failures = failures[:MAX_VALIDATION_FAILURES]
    details = "; ".join(
        f"{_bounded_text(failure.path, MAX_FAILURE_PATH_CHARS)}: "
        f"{_bounded_text(failure.message, MAX_FAILURE_MESSAGE_CHARS)}"
        for failure in bounded_failures
    )
    instruction = (
        "Return only JSON that satisfies the requested schema. "
        f"Correct these validation failures: {details}."
    )
    corrected["input"].append({"role": "user", "content": instruction})
    return corrected


def _failure_path(
    path_parts: Any,
    validator: str | None = None,
    raw_message: str = "",
) -> str:
    parts = list(path_parts)
    if validator == "required":
        match = re.match(r"^'([^']+)' is a required property$", raw_message)
        if match:
            parts.append(match.group(1))

    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def _failure_message(validator: str | None, validator_value: Any) -> str:
    if validator == "type":
        if isinstance(validator_value, list):
            expected = " or ".join(str(value) for value in validator_value)
        else:
            expected = str(validator_value)
        return f"expected {expected}"
    if validator == "required":
        return "required property"
    if validator == "enum":
        return "value is not allowed"
    if validator == "const":
        return "value does not match required constant"
    if validator == "additionalProperties":
        return "additional property is not allowed"
    return "schema constraint failed"


def _contains_remote_reference(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"$ref", "$dynamicRef"} and isinstance(nested, str):
                if not nested.startswith("#"):
                    return True
            if _contains_remote_reference(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_remote_reference(item) for item in value)
    return False


def _reject_json_constant(_value: str) -> None:
    raise ValueError("Non-standard JSON constant")


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."
