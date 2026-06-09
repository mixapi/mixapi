# MixAPI Reliability And Governance Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the remaining bounded P0 gaps for fallback errors, spend budgets, circuit breaking, and exportable usage.

**Architecture:** Keep the modular-monolith shape and add small in-memory services behind explicit interfaces. The API edge remains responsible for public error mapping, while dispatch helpers report provider failure reasons, a budget service reserves and reconciles spend, and a circuit service controls candidate eligibility. Usage export reads the existing tenant-scoped ledger and serializes it without introducing a second source of truth.

**Tech Stack:** Python 3.14, FastAPI, standard-library `decimal`, `csv`, and `io`, pytest/unittest, existing MixAPI adapters and stores.

### Task 1: Normalize Exhausted Provider Failures

**Files:**
- Modify: `mixapi/errors.py`
- Modify: `mixapi/app.py`
- Modify: `tests/test_fallbacks.py`

**Steps:**
1. Add failing response and embedding tests for exhausted 429 and timeout attempts.
2. Run the focused tests and confirm they fail with the current 503 response.
3. Add `provider_rate_limited` and `upstream_timeout` error factories.
4. Map `AllCandidatesFailed.failed_attempts` to the most specific public error: timeout when every attempt timed out, rate-limited when every attempt was a 429, otherwise unavailable.
5. Run the focused tests and the full suite.
6. Mark Task 1 complete in `TASKS.md`.

### Task 2: Enforce Spend Budgets Before Dispatch

**Files:**
- Create: `mixapi/budget.py`
- Modify: `mixapi/errors.py`
- Modify: `mixapi/app.py`
- Modify: `mixapi/validation.py`
- Create: `tests/test_budgets.py`

**Steps:**
1. Add failing tests proving `routing.max_cost_usd` and API-key cumulative budgets reject requests before adapter dispatch.
2. Add validation for positive decimal request budget values.
3. Add `budget_exceeded` and an in-memory reservation service keyed by API key.
4. Estimate each request from normalized input tokens, requested maximum output, and candidate prices; reject when no candidate fits the request budget.
5. Reserve the highest planned candidate estimate before dispatch and reconcile to actual cost after success; release the reservation when every candidate fails.
6. Run focused and full tests.
7. Mark Task 2 complete in `TASKS.md`.

### Task 3: Add Provider-Model Circuit Breakers

**Files:**
- Create: `mixapi/circuits.py`
- Modify: `mixapi/app.py`
- Modify: `mixapi/route_decisions.py`
- Create: `tests/test_circuit_breakers.py`

**Steps:**
1. Add failing tests showing repeated transient failures open a circuit and subsequent requests skip that provider-model.
2. Implement an in-memory circuit service with configurable failure threshold and recovery timeout.
3. Filter open candidates before dispatch and append `provider_circuit_open` to rejected candidate metadata.
4. Record transient failures and reset circuit state after a successful dispatch.
5. Return `provider_unavailable` without dispatch when all otherwise eligible candidates have open circuits.
6. Run focused and full tests.
7. Mark Task 3 complete in `TASKS.md`.

### Task 4: Export Tenant-Scoped Usage As CSV

**Files:**
- Modify: `mixapi/usage.py`
- Modify: `mixapi/app.py`
- Modify: `tests/test_usage_endpoint.py`

**Steps:**
1. Add failing tests for CSV headers, rows, content type, disposition, and tenant isolation.
2. Add a CSV serializer over `UsageEvent.public_dict()` using `csv.DictWriter`.
3. Add `GET /v1/usage/export?format=csv`, preserving authentication and tenant scoping.
4. Reject unsupported export formats with the normalized validation envelope.
5. Run focused and full tests.
6. Mark Task 4 and verification complete in `TASKS.md`.

### Task 5: Final Verification

**Files:**
- Modify: `TASKS.md`

**Steps:**
1. Run `python3 -m unittest discover -s tests -v`.
2. Run `python3 -m compileall mixapi tests`.
3. Run a whitespace scan and inspect repository status.
4. Review changed code for contract regressions and update remaining task statuses.
