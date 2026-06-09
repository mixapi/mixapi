# MixAPI Request Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add idempotency, fallback execution, and route-decision persistence to the MixAPI MVP request path.

**Architecture:** Keep the existing FastAPI modular monolith and in-memory stores. Add small focused modules for idempotency and route decisions, extend the deterministic adapter to simulate provider failures, and keep the public API handlers thin by moving shared response execution into helpers.

**Tech Stack:** Python 3.14, FastAPI, Pydantic v2, standard-library `unittest`, FastAPI `TestClient`.

### Task 1: Idempotency

**Files:**
- Create: `mixapi/idempotency.py`
- Modify: `mixapi/app.py`
- Test: `tests/test_idempotency.py`

**Steps:**
1. Write failing tests for repeated `Idempotency-Key` returning the first response without adding usage, and reused keys with different bodies returning `validation_error`.
2. Run `python3 -m unittest tests.test_idempotency -v` and verify failures.
3. Implement an in-memory idempotency store keyed by tenant, endpoint, and key.
4. Wire idempotency into `/v1/responses` and `/v1/embeddings`.
5. Run the focused tests and verify they pass.

### Task 2: Fallback Execution

**Files:**
- Modify: `mixapi/adapters.py`
- Modify: `mixapi/routing.py`
- Modify: `mixapi/errors.py`
- Modify: `mixapi/app.py`
- Test: `tests/test_fallbacks.py`

**Steps:**
1. Write failing tests for fallback from a failed lowest-cost provider and provider-unavailable when all eligible providers fail.
2. Run `python3 -m unittest tests.test_fallbacks -v` and verify failures.
3. Add deterministic provider failures through `create_app(failed_response_providers=...)`.
4. Extend routing to return ordered eligible candidates.
5. Execute candidates until one succeeds or the fallback budget is exhausted.
6. Run the focused tests and verify they pass.

### Task 3: Route Decision Persistence

**Files:**
- Create: `mixapi/route_decisions.py`
- Modify: `mixapi/app.py`
- Test: `tests/test_route_decisions.py`

**Steps:**
1. Write failing tests for persisted route decisions and `GET /v1/route-decisions/{request_id}`.
2. Run `python3 -m unittest tests.test_route_decisions -v` and verify failures.
3. Implement in-memory route decision storage.
4. Persist selected provider, rejected candidates, attempts, fallback status, and final status.
5. Add the route decision read endpoint.
6. Run the focused tests and verify they pass.

### Task 4: Full Verification

**Files:**
- Modify as needed based on failures.

**Steps:**
1. Run `python3 -m unittest discover -v`.
2. Run `python3 -m compileall mixapi tests`.
3. Run `git diff --check`.
4. Start the local API and smoke-test `/v1/responses`.

