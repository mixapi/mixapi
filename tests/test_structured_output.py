import unittest

from mixapi.structured_output import (
    InvalidResponseSchema,
    ValidationFailure,
    check_response_schema,
    corrective_request,
    response_schema,
    validate_output,
)


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


if __name__ == "__main__":
    unittest.main()
