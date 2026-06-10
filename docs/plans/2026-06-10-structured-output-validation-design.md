# Structured Output Validation Design

## Status

Approved for implementation on June 10, 2026.

## Goal

Add provider-neutral JSON Schema validation, corrective retry, and fallback behavior for non-streaming `/v1/responses` requests that use `response.format.type = "json_schema"`.

## Scope

This increment supports JSON Schema Draft 2020-12 through the `jsonschema` package. MixAPI validates request schemas before routing and validates generated JSON before returning it. Strict structured output remains non-streaming in this increment.

Out of scope:

- Streaming structured-output validation.
- Provider-specific schema dialects.
- Returning or storing invalid generated payloads in public errors or corrective prompts.
- Arbitrary configurable retry counts.

## Architecture

Add `mixapi/structured_output.py` as the provider-neutral validation boundary. It will:

1. Extract `response.format.json_schema` from a request.
2. Validate the supplied schema with `Draft202012Validator.check_schema()`.
3. Parse provider `output_text` as JSON.
4. Validate the parsed value and produce bounded path-based failures.
5. Build a corrective internal request containing concise validation failures.

Adapters continue translating portable structured-output settings to provider APIs. They do not own response validation or retry policy.

The non-streaming dispatcher treats schema validation as part of a provider attempt. Each candidate receives one normal dispatch and, only after invalid structured output, one corrective retry. If both generated values fail validation, fallback continues to the next routed candidate. Provider transport failures retain existing fallback behavior and do not receive schema retries.

## Request Validation

Request validation occurs before route planning, budget reservation, quota reservation, or provider dispatch.

- `json_schema` must be an object and a valid Draft 2020-12 schema.
- Invalid schemas return `400 validation_error` with code `invalid_json_schema`.
- `json_schema` combined with `stream: true` returns `400 capability_unsupported` with code `streaming_structured_output_unsupported`.
- Requests without `json_schema` retain existing behavior.

## Dispatch Flow

For each eligible candidate:

1. Dispatch the original request.
2. Parse and validate `output_text`.
3. Return immediately when valid.
4. When invalid, record a failed route attempt with reason `schema_validation_failed`.
5. Create a deep-copied internal request with a corrective instruction.
6. Dispatch once more to the same candidate.
7. Return when the retry is valid; otherwise record the retry failure and continue to the next candidate.

The corrective instruction includes a bounded list of JSON paths and validation messages, such as `$.customer.email: expected string`. It never includes the invalid generated payload. The original request remains unchanged for idempotency hashing and audit behavior.

The dispatcher returns a richer outcome containing:

- The accepted adapter response.
- The selected candidate.
- Failed route attempts, including structured-output failures.
- Billable dispatch records for accounting.

Repeated provider/model entries in route metadata are valid because a corrective retry is a distinct provider dispatch.

## Accounting

Structured-output requests reserve budget and token quota for the worst-case dispatch sequence: up to two dispatches for each eligible candidate.

Reconciliation aggregates input tokens, output tokens, and cost from every provider response received, including invalid initial output and corrective retries. Transport failures without provider usage metadata contribute no billable usage.

The usage ledger records one event per billable provider dispatch to preserve provider and model attribution. The public response reports aggregate usage and cost across all attempts while returning only the accepted output.

Idempotency stores the final accepted response. A replay returns the stored response without validation retries or provider dispatch.

## Failure Semantics

When all candidates and corrective retries produce invalid output, MixAPI returns:

- HTTP status: `422`
- Error type: `structured_output_error`
- Error code: `schema_validation_failed`

The error message contains only a bounded provider/model attempt summary and a bounded set of validation paths. It does not expose generated content.

Schema validation failures do not count as provider transport failures and do not open circuit breakers.

## Observability

Each validation emits a `structured_output.validate` span with:

- Provider.
- Provider model.
- Retry number.
- Validation status.
- Failure class when invalid.

Invalid output increments `mixapi_structured_output_failures_total`. Existing provider dispatch spans continue to represent every network dispatch.

No prompt, schema payload, generated output, or corrective instruction is included in observability attributes.

## OpenAPI Contract

The explicit contract will document:

- Draft 2020-12 schema input under `response.format.json_schema`.
- The streaming incompatibility.
- The normalized `422 structured_output_error` response.

The checked-in OpenAPI artifact and SDK fixtures must remain deterministic and synchronized with the contract builder.

## Testing

Focused tests will cover:

- Valid and invalid Draft 2020-12 request schemas.
- Streaming rejection before provider dispatch.
- Initial valid JSON acceptance.
- Invalid JSON and schema mismatches causing one corrective retry.
- Corrective instructions containing bounded paths but not invalid output.
- Retry success with aggregate usage, cost, and route metadata.
- Fallback after both attempts fail validation.
- Normalized 422 behavior when all candidates fail.
- Schema failures leaving circuit state unchanged.
- One usage event per billable dispatch.
- Idempotency replay without redispatch.
- Observability counters and spans.
- OpenAPI and fixture synchronization.

Completion requires the focused tests, full test suite, Python compilation, OpenAPI regeneration check, and whitespace validation to pass.
