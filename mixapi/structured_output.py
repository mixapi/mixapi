from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


MAX_VALIDATION_FAILURES = 3


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
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise InvalidResponseSchema("JSON Schema is not valid Draft 2020-12.") from error
    return schema


def validate_output(output_text: str, schema: dict[str, Any]) -> StructuredOutputValidation:
    try:
        value = json.loads(output_text)
    except (json.JSONDecodeError, TypeError):
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

    failures = tuple(
        ValidationFailure(
            path=_failure_path(error.absolute_path, error.validator, error.message),
            message=_failure_message(error.validator, error.validator_value),
        )
        for error in errors[:MAX_VALIDATION_FAILURES]
    )
    return StructuredOutputValidation(valid=False, failures=failures)


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

    details = "; ".join(f"{failure.path}: {failure.message}" for failure in failures)
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
