# Structured Output Validation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Validate non-streaming JSON Schema responses, retry invalid output once on the same provider, fall back across providers, and account for every billable dispatch.

**Architecture:** A provider-neutral `mixapi/structured_output.py` module owns Draft 2020-12 schema checks, output parsing, bounded validation failures, and corrective request construction. The response dispatcher returns a structured outcome containing accepted output, route failures, and every billable provider response so the endpoint can reconcile quota, budget, usage, and route records consistently.

**Tech Stack:** Python 3.14, FastAPI, `jsonschema`, Pydantic v2, `unittest`, deterministic OpenAPI generation.

### Task 1: Add The Draft 2020-12 Validation Core

**Files:**
- Modify: `pyproject.toml`
- Create: `mixapi/structured_output.py`
- Create: `tests/test_structured_output.py`

**Step 1: Write failing unit tests**

Cover:

- `response_schema()` returns `None` for ordinary requests and the schema object for `json_schema` requests.
- `check_response_schema()` accepts a valid Draft 2020-12 schema and raises `invalid_json_schema` for malformed schemas.
- `validate_output()` accepts valid JSON and reports invalid JSON as `$: invalid JSON`.
- Schema mismatches are sorted and bounded to three path-based failures.
- `corrective_request()` deep-copies the request, preserves the original object, appends a user instruction, includes validation paths, and excludes invalid output.

Use a result model with explicit success and failure values:

```python
@dataclass(frozen=True)
class ValidationFailure:
    path: str
    message: str


@dataclass(frozen=True)
class StructuredOutputValidation:
    valid: bool
    value: Any | None = None
    failures: tuple[ValidationFailure, ...] = ()
```

**Step 2: Run tests to verify red**

Run: `python -m unittest tests.test_structured_output -v`

Expected: FAIL because `mixapi.structured_output` does not exist.

**Step 3: Add dependency and minimal implementation**

Add `jsonschema>=4.25.0` to `pyproject.toml`. Implement with `Draft202012Validator`, `SchemaError`, `json.loads`, `copy.deepcopy`, and a maximum of three public failure details. Format paths as `$`, `$.field`, and `$[0]`.

The corrective request must normalize string input to a user message list and append:

```text
Return only JSON that satisfies the requested schema. Correct these validation failures: <bounded failures>.
```

Do not include the rejected generated output.

**Step 4: Run tests to verify green**

Run: `python -m unittest tests.test_structured_output -v`

Expected: all structured-output unit tests pass.

**Step 5: Commit**

```bash
git add pyproject.toml mixapi/structured_output.py tests/test_structured_output.py
git commit -m "feat: add structured output validator"
```

### Task 2: Reject Invalid Schemas And Streaming Before Dispatch

**Files:**
- Modify: `mixapi/validation.py`
- Modify: `mixapi/errors.py`
- Modify: `tests/test_request_validation.py`

**Step 1: Write failing endpoint tests**

Add tests proving:

- A malformed schema returns `400 validation_error / invalid_json_schema`.
- A non-object `json_schema` returns the same normalized error.
- `stream: true` with `json_schema` returns `400 capability_unsupported / streaming_structured_output_unsupported`.
- Provider dispatch is not called in any of these cases by patching `DeterministicProviderAdapter.dispatch_response`.

**Step 2: Run tests to verify red**

Run: `python -m unittest tests.test_request_validation -v`

Expected: new tests fail because request schemas are not checked and streaming is accepted.

**Step 3: Implement request gating**

Call the structured-output schema checker from `validate_response_request()` after the existing stream type check. Reject streaming before route planning. Convert `jsonschema.exceptions.SchemaError` into the existing normalized validation error rather than exposing library text.

Add a `structured_output_error(code, message)` constructor in `mixapi/errors.py` returning status `422`; later tasks use it for exhausted validation failures.

**Step 4: Run focused tests**

Run: `python -m unittest tests.test_request_validation tests.test_structured_output -v`

Expected: all focused tests pass.

**Step 5: Commit**

```bash
git add mixapi/validation.py mixapi/errors.py tests/test_request_validation.py
git commit -m "feat: validate structured output requests"
```

### Task 3: Retry Invalid Output And Fall Back

**Files:**
- Modify: `mixapi/app.py`
- Modify: `tests/test_structured_output.py`

**Step 1: Write failing dispatcher tests**

Patch `DeterministicProviderAdapter.dispatch_response` with controlled `AdapterResponse` sequences and test through `/v1/responses`:

- Initial valid JSON succeeds with one dispatch.
- Invalid JSON followed by valid JSON retries the same provider exactly once.
- The retry request contains the corrective instruction and does not contain the invalid output.
- Two invalid responses on OpenAI fall back to Gemini.
- Every candidate returning invalid output returns `422 structured_output_error / schema_validation_failed`.
- Schema validation failures do not call `circuits.record_failure()` and leave the circuit closed.

**Step 2: Run tests to verify red**

Run: `python -m unittest tests.test_structured_output -v`

Expected: output is currently returned without validation or retry.

**Step 3: Introduce structured dispatch outcomes**

Add internal immutable records in `mixapi/app.py`:

```python
@dataclass(frozen=True)
class BillableDispatch:
    candidate: ProviderModel
    response: AdapterResponse


@dataclass(frozen=True)
class ResponseDispatchOutcome:
    response: AdapterResponse
    selected_candidate: ProviderModel
    failed_attempts: tuple[dict[str, str], ...]
    billable_dispatches: tuple[BillableDispatch, ...]
```

Extend `AllCandidatesFailed` to carry billable dispatches. Update `_dispatch_response_with_fallback()` to:

1. Dispatch normally.
2. Record transport success and circuit success.
3. Validate output when a schema is present.
4. Record `schema_validation_failed` without recording a circuit failure.
5. Retry once with `corrective_request()`.
6. Continue to the next candidate after a second validation failure.

Keep the non-structured path behavior unchanged.

When every failed attempt reason is `schema_validation_failed`, `_public_error_for_failed_attempts()` must return the normalized 422 error with a bounded message. Mixed transport/schema exhaustion retains the existing provider-unavailable classification.

**Step 4: Run focused tests**

Run: `python -m unittest tests.test_structured_output tests.test_fallbacks -v`

Expected: retry/fallback tests and existing fallback tests pass.

**Step 5: Commit**

```bash
git add mixapi/app.py tests/test_structured_output.py
git commit -m "feat: retry invalid structured responses"
```

### Task 4: Account For Every Billable Dispatch

**Files:**
- Modify: `mixapi/app.py`
- Modify: `tests/test_structured_output.py`
- Modify: `tests/test_budgets.py`
- Modify: `tests/test_quota_usage_embeddings.py`

**Step 1: Write failing accounting tests**

Cover:

- Retry success reports aggregate input/output tokens and aggregate cost.
- Retry success writes two usage events with the same request ID and correct provider/model attribution.
- Fallback after two invalid OpenAI responses and one valid Gemini response writes three usage events.
- All-invalid 422 responses still reconcile reservations and persist billable usage.
- Structured requests reserve enough token quota and spend budget for two dispatches per eligible candidate; denial occurs before provider dispatch.
- Ordinary requests retain their existing reservation behavior.

**Step 2: Run tests to verify red**

Run: `python -m unittest tests.test_structured_output tests.test_budgets tests.test_quota_usage_embeddings -v`

Expected: retry usage is missing and public totals include only the selected response.

**Step 3: Implement aggregate accounting**

Add helpers that:

- Compute cost per `BillableDispatch` using that dispatch's candidate pricing.
- Sum tokens and costs for the public response.
- Write one `UsageEvent` per billable dispatch.
- Reconcile budget and token quota on both success and exhausted structured-output failure.

Change structured-output reservation estimates to cover two dispatches for every eligible candidate. Keep existing one-dispatch estimates for ordinary response and embedding requests. Request-level `max_cost_usd` must reject a structured request when its worst-case structured dispatch estimate exceeds the limit.

The accepted output and tool calls come only from the selected response; only usage and cost are aggregate.

**Step 4: Run focused tests**

Run: `python -m unittest tests.test_structured_output tests.test_budgets tests.test_quota_usage_embeddings tests.test_persistence -v`

Expected: accounting and persistence tests pass.

**Step 5: Commit**

```bash
git add mixapi/app.py tests/test_structured_output.py tests/test_budgets.py tests/test_quota_usage_embeddings.py
git commit -m "feat: account for structured output retries"
```

### Task 5: Add Structured Validation Observability And Idempotency Coverage

**Files:**
- Modify: `mixapi/app.py`
- Modify: `tests/test_observability.py`
- Modify: `tests/test_idempotency.py`

**Step 1: Write failing tests**

Verify:

- Each validation emits `structured_output.validate` with provider, provider model, retry number, status, and bounded error class.
- Invalid output increments `mixapi_structured_output_failures_total`.
- Spans and labels contain no request schema, prompt, generated output, or corrective instruction.
- A successful structured response stored under an idempotency key replays without additional dispatch, validation spans, or usage events.

**Step 2: Run tests to verify red**

Run: `python -m unittest tests.test_observability tests.test_idempotency -v`

Expected: structured validation metrics and spans are absent.

**Step 3: Add instrumentation**

Record one validation span per generated response. Use status `ok` or `error`; attributes are limited to endpoint, provider, provider model, retry number, and error class. Increment the failure counter only for invalid generated output. Do not change provider dispatch spans.

Ensure idempotency replay remains before route planning and dispatch.

**Step 4: Run focused tests**

Run: `python -m unittest tests.test_observability tests.test_idempotency tests.test_structured_output -v`

Expected: all focused tests pass.

**Step 5: Commit**

```bash
git add mixapi/app.py tests/test_observability.py tests/test_idempotency.py
git commit -m "feat: observe structured output validation"
```

### Task 6: Publish The Updated API Contract And SDK Fixtures

**Files:**
- Modify: `mixapi/api_contract.py`
- Modify: `tests/test_openapi_contract.py`
- Modify: `sdk-fixtures/requests/response-create.json`
- Modify: `sdk-fixtures/errors/error-envelope.json`
- Modify: `openapi/openapi.json`

**Step 1: Write failing contract tests**

Assert that:

- `ResponseFormat.json_schema` is documented as a Draft 2020-12 schema object.
- `/v1/responses` documents normalized 422 errors with `ErrorEnvelope`.
- The response request fixture contains a representative strict schema.
- The error fixture can represent `structured_output_error / schema_validation_failed`.

**Step 2: Run tests to verify red**

Run: `python -m unittest tests.test_openapi_contract tests.test_sdk_fixtures -v`

Expected: 422 documentation and fixture expectations fail.

**Step 3: Update contract and fixtures**

Add the 422 response to the operation contract builder without changing the generic error envelope. Add schema descriptions clarifying Draft 2020-12 and non-streaming validation. Update fixtures and regenerate deterministically:

Run: `python scripts/generate_openapi.py`

**Step 4: Run focused tests**

Run: `python -m unittest tests.test_openapi_contract tests.test_sdk_fixtures -v`

Expected: contract and fixture tests pass.

**Step 5: Commit**

```bash
git add mixapi/api_contract.py tests/test_openapi_contract.py sdk-fixtures openapi/openapi.json
git commit -m "docs: publish structured output contract"
```

### Task 7: Close The P1 Task And Verify The Branch

**Files:**
- Modify: `TASKS.md`

**Step 1: Update the backlog**

Add a P1 execution section if absent and mark provider-aware JSON Schema validation, corrective retry, fallback, accounting, observability, and contract coverage complete only after verification.

**Step 2: Run full verification**

Run:

```bash
python -m unittest discover -s tests -v
python -m compileall -q mixapi tests scripts
python scripts/generate_openapi.py
git diff --exit-code -- openapi/openapi.json
git diff --check
```

Expected: all tests pass, compilation succeeds, regeneration produces no diff, and whitespace checks pass.

**Step 3: Review the implementation against the approved design**

Check every requirement in `docs/plans/2026-06-10-structured-output-validation-design.md`, inspect `git diff master...HEAD`, and fix any missing behavior with a red-green test before proceeding.

**Step 4: Commit task completion**

```bash
git add TASKS.md
git commit -m "docs: complete structured output validation task"
```

**Step 5: Run final verification after the last commit**

Repeat the full verification commands and confirm `git status --short` is empty.
