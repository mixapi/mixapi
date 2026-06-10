import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mixapi.adapters import AdapterResponse, DeterministicProviderAdapter
from mixapi.app import create_app
from mixapi.circuits import InMemoryCircuitBreaker
from mixapi.structured_output import (
    InvalidResponseSchema,
    ValidationFailure,
    check_response_schema,
    corrective_request,
    response_schema,
    validate_output,
)


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


class StructuredOutputUnitTest(unittest.TestCase):
    def test_response_schema_extracts_only_json_schema_requests(self) -> None:
        self.assertIsNone(response_schema({"response": {"format": {"type": "text"}}}))

        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

        self.assertIs(
            response_schema(
                {"response": {"format": {"type": "json_schema", "json_schema": schema}}}
            ),
            schema,
        )

    def test_check_response_schema_accepts_draft_2020_12_schema(self) -> None:
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }

        self.assertIs(check_response_schema(schema), schema)

    def test_check_response_schema_rejects_non_object_and_malformed_schema(self) -> None:
        for schema in ("not-an-object", {"type": 7}):
            with self.subTest(schema=schema):
                with self.assertRaises(InvalidResponseSchema):
                    check_response_schema(schema)

    def test_check_response_schema_rejects_remote_references(self) -> None:
        for keyword in ("$ref", "$dynamicRef"):
            with self.subTest(keyword=keyword):
                with self.assertRaises(InvalidResponseSchema):
                    check_response_schema(
                        {keyword: "https://schemas.example.com/customer.json"}
                    )

    def test_validate_output_parses_and_validates_json(self) -> None:
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }

        result = validate_output('{"ok":true}', schema)

        self.assertTrue(result.valid)
        self.assertEqual(result.value, {"ok": True})
        self.assertEqual(result.failures, ())

    def test_validate_output_reports_invalid_json_without_echoing_output(self) -> None:
        invalid_output = "private invalid output"

        result = validate_output(invalid_output, {"type": "object"})

        self.assertFalse(result.valid)
        self.assertEqual(result.failures, (ValidationFailure("$", "invalid JSON"),))
        self.assertNotIn(invalid_output, repr(result))

    def test_validate_output_rejects_nonstandard_json_constants(self) -> None:
        for output in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(output=output):
                result = validate_output(output, {})

                self.assertFalse(result.valid)
                self.assertEqual(
                    result.failures,
                    (ValidationFailure("$", "invalid JSON"),),
                )

    def test_validate_output_sorts_and_bounds_path_failures(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "d": {"type": "string"},
                "b": {"type": "string"},
                "a": {"type": "string"},
                "c": {"type": "string"},
            },
        }

        result = validate_output('{"d":4,"b":2,"a":1,"c":3}', schema)

        self.assertFalse(result.valid)
        self.assertEqual(
            result.failures,
            (
                ValidationFailure("$.a", "expected string"),
                ValidationFailure("$.b", "expected string"),
                ValidationFailure("$.c", "expected string"),
            ),
        )

    def test_corrective_request_preserves_original_and_appends_bounded_failures(self) -> None:
        request_body = {
            "model": "mixapi/balanced-chat",
            "input": "Return customer JSON",
            "response": {
                "format": {
                    "type": "json_schema",
                    "json_schema": {"type": "object"},
                }
            },
        }
        original = {
            "model": "mixapi/balanced-chat",
            "input": "Return customer JSON",
            "response": {
                "format": {
                    "type": "json_schema",
                    "json_schema": {"type": "object"},
                }
            },
        }

        corrected = corrective_request(
            request_body,
            (
                ValidationFailure("$.customer.email", "expected string"),
                ValidationFailure("$.customer.name", "required property"),
            ),
        )

        self.assertEqual(request_body, original)
        self.assertIsNot(corrected, request_body)
        self.assertEqual(corrected["input"][0]["content"], "Return customer JSON")
        instruction = corrected["input"][-1]["content"]
        self.assertIn("Return only JSON", instruction)
        self.assertIn("$.customer.email: expected string", instruction)
        self.assertIn("$.customer.name: required property", instruction)
        self.assertNotIn("private invalid output", repr(corrected))

    def test_validation_paths_and_corrective_instruction_are_character_bounded(self) -> None:
        property_name = "field " * 100
        schema = {
            "type": "object",
            "properties": {property_name: {"type": "string"}},
        }

        result = validate_output(json.dumps({property_name: 7}), schema)
        corrected = corrective_request(
            {"input": "Return JSON"},
            result.failures,
        )

        self.assertFalse(result.valid)
        self.assertLessEqual(len(result.failures[0].path), 160)
        self.assertLessEqual(len(result.failures[0].message), 80)
        self.assertLessEqual(len(corrected["input"][-1]["content"]), 800)


class StructuredOutputEndpointTest(unittest.TestCase):
    def test_valid_output_is_accepted_without_retry(self) -> None:
        client = TestClient(create_app())

        with patch.object(
            DeterministicProviderAdapter,
            "dispatch_response",
            autospec=True,
            return_value=AdapterResponse('{"ok":true}', 2, 1),
        ) as dispatch:
            response = client.post(
                "/v1/responses",
                headers=AUTH_HEADERS,
                json=_strict_request(provider="openai"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output_text"], '{"ok":true}')
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(response.json()["route"]["attempts"], 1)

    def test_invalid_output_retries_same_provider_with_corrective_request(self) -> None:
        requests: list[dict[str, object]] = []
        providers: list[str] = []

        def dispatch(_adapter, request_body, candidate):
            requests.append(request_body)
            providers.append(candidate.provider)
            output = "not-json" if len(requests) == 1 else '{"ok":true}'
            return AdapterResponse(output, 2, 1)

        app = create_app()
        client = TestClient(app)
        with (
            patch.object(
                DeterministicProviderAdapter,
                "dispatch_response",
                autospec=True,
                side_effect=dispatch,
            ) as provider_dispatch,
            patch.object(
                InMemoryCircuitBreaker,
                "record_failure",
                autospec=True,
            ) as record_failure,
        ):
            response = client.post(
                "/v1/responses",
                headers=AUTH_HEADERS,
                json=_strict_request(provider="openai"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider_dispatch.call_count, 2)
        self.assertEqual(providers, ["openai", "openai"])
        correction = requests[1]["input"][-1]["content"]
        self.assertIn("Return only JSON", correction)
        self.assertIn("$: invalid JSON", correction)
        self.assertNotIn("not-json", correction)
        route = response.json()["route"]
        self.assertEqual(route["attempts"], 2)
        self.assertTrue(route["fallback_used"])
        self.assertEqual(route["failed_attempts"][0]["reason"], "schema_validation_failed")
        record_failure.assert_not_called()
        self.assertFalse(app.state.circuits.is_open("openai", "gpt-4.1-mini"))

    def test_two_invalid_outputs_fall_back_to_next_provider(self) -> None:
        providers: list[str] = []

        def dispatch(_adapter, _request_body, candidate):
            providers.append(candidate.provider)
            output = '{"ok":true}' if candidate.provider == "gemini" else "not-json"
            return AdapterResponse(output, 2, 1)

        client = TestClient(create_app())
        with patch.object(
            DeterministicProviderAdapter,
            "dispatch_response",
            autospec=True,
            side_effect=dispatch,
        ):
            response = client.post(
                "/v1/responses",
                headers=AUTH_HEADERS,
                json=_strict_request(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(providers, ["openai", "openai", "gemini"])
        self.assertEqual(response.json()["provider"], "gemini")
        self.assertEqual(response.json()["route"]["attempts"], 3)
        self.assertEqual(
            [attempt["provider"] for attempt in response.json()["route"]["failed_attempts"]],
            ["openai", "openai"],
        )

    def test_all_invalid_outputs_return_structured_output_error(self) -> None:
        app = create_app()
        client = TestClient(app)

        with patch.object(
            DeterministicProviderAdapter,
            "dispatch_response",
            autospec=True,
            return_value=AdapterResponse('{"ok":"wrong"}', 2, 1),
        ) as dispatch:
            response = client.post(
                "/v1/responses",
                headers=AUTH_HEADERS,
                json=_strict_request(),
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["type"], "structured_output_error")
        self.assertEqual(response.json()["error"]["code"], "schema_validation_failed")
        self.assertIn("$.ok", response.json()["error"]["message"])
        self.assertIn("openai/gpt-4.1-mini", response.json()["error"]["message"])
        self.assertIn("gemini/gemini-2.5-flash", response.json()["error"]["message"])
        self.assertEqual(dispatch.call_count, 4)
        self.assertEqual(len(app.state.usage_ledger.events()), 4)
        self.assertFalse(app.state.circuits.is_open("openai", "gpt-4.1-mini"))
        self.assertFalse(app.state.circuits.is_open("gemini", "gemini-2.5-flash"))

    def test_retry_success_aggregates_usage_cost_and_ledger_events(self) -> None:
        responses = iter(
            (
                AdapterResponse("not-json", 2, 3),
                AdapterResponse('{"ok":true}', 4, 5),
            )
        )
        app = create_app()
        client = TestClient(app)

        with patch.object(
            DeterministicProviderAdapter,
            "dispatch_response",
            autospec=True,
            side_effect=lambda *_args: next(responses),
        ):
            response = client.post(
                "/v1/responses",
                headers={**AUTH_HEADERS, "X-Request-ID": "req_structured_retry"},
                json=_strict_request(provider="openai"),
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["usage"]["input_tokens"], 6)
        self.assertEqual(body["usage"]["output_tokens"], 8)
        self.assertEqual(body["cost"]["provider_cost_usd"], "0.00001520")
        events = app.state.usage_ledger.events()
        self.assertEqual(len(events), 2)
        self.assertEqual([event.request_id for event in events], ["req_structured_retry"] * 2)
        self.assertEqual([event.input_tokens for event in events], [2, 4])
        self.assertEqual([event.output_tokens for event in events], [3, 5])
        self.assertEqual([event.provider for event in events], ["openai", "openai"])

    def test_fallback_records_each_billable_provider_dispatch(self) -> None:
        def dispatch(_adapter, _request_body, candidate):
            if candidate.provider == "openai":
                return AdapterResponse("not-json", 1, 1)
            return AdapterResponse('{"ok":true}', 2, 2)

        app = create_app()
        client = TestClient(app)
        with patch.object(
            DeterministicProviderAdapter,
            "dispatch_response",
            autospec=True,
            side_effect=dispatch,
        ):
            response = client.post(
                "/v1/responses",
                headers=AUTH_HEADERS,
                json=_strict_request(),
            )

        self.assertEqual(response.status_code, 200)
        events = app.state.usage_ledger.events()
        self.assertEqual([event.provider for event in events], ["openai", "openai", "gemini"])
        self.assertEqual(response.json()["usage"]["input_tokens"], 4)
        self.assertEqual(response.json()["usage"]["output_tokens"], 4)


def _strict_request(provider: str | None = None) -> dict[str, object]:
    request: dict[str, object] = {
        "model": "mixapi/balanced-chat",
        "input": "Return JSON",
        "max_output_tokens": 8,
        "response": {
            "format": {
                "type": "json_schema",
                "json_schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            }
        },
    }
    if provider is not None:
        request["native"] = {"provider": provider}
    return request


if __name__ == "__main__":
    unittest.main()
