# MixAPI MVP Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the first runnable MixAPI gateway slice with model discovery, responses, embeddings, capability-aware routing, quotas, usage events, and normalized errors.

**Architecture:** Use a Python FastAPI modular monolith with in-memory repositories for the MVP. Keep public API, catalog, routing, adapters, quota, usage, and auth in separate modules so PostgreSQL, Redis, and real provider adapters can replace in-memory pieces later.

**Tech Stack:** Python 3.14, FastAPI, Pydantic v2, standard-library `unittest`, FastAPI `TestClient`.

### Task 1: Project Skeleton And Model Discovery

**Files:**
- Create: `pyproject.toml`
- Create: `mixapi/__init__.py`
- Create: `mixapi/models.py`
- Create: `mixapi/catalog.py`
- Create: `mixapi/app.py`
- Test: `tests/test_models_endpoint.py`

**Steps:**
1. Write a failing API test for authenticated `GET /v1/models`.
2. Run `python3 -m unittest tests.test_models_endpoint -v` and verify it fails because the app does not exist.
3. Implement the minimal app, catalog, and model data structures.
4. Run the same test and verify it passes.

### Task 2: Authentication And Error Envelope

**Files:**
- Modify: `mixapi/app.py`
- Create: `mixapi/auth.py`
- Create: `mixapi/errors.py`
- Test: `tests/test_auth_and_errors.py`

**Steps:**
1. Write failing tests for missing API key, invalid API key, and normalized error shape.
2. Run `python3 -m unittest tests.test_auth_and_errors -v` and verify failures.
3. Implement API-key auth using a default development key plus `MIXAPI_API_KEYS`.
4. Implement normalized error responses.
5. Run the tests and verify they pass.

### Task 3: Responses Routing And Capability Checks

**Files:**
- Create: `mixapi/routing.py`
- Create: `mixapi/adapters.py`
- Modify: `mixapi/app.py`
- Test: `tests/test_responses_endpoint.py`

**Steps:**
1. Write failing tests for `POST /v1/responses`, capability rejection, provider pinning, and route metadata.
2. Run `python3 -m unittest tests.test_responses_endpoint -v` and verify failures.
3. Implement route eligibility filters and deterministic provider adapters.
4. Run the tests and verify they pass.

### Task 4: Quotas, Usage, And Embeddings

**Files:**
- Create: `mixapi/quota.py`
- Create: `mixapi/usage.py`
- Modify: `mixapi/app.py`
- Test: `tests/test_quota_usage_embeddings.py`

**Steps:**
1. Write failing tests for quota denial, usage recording, and `POST /v1/embeddings`.
2. Run `python3 -m unittest tests.test_quota_usage_embeddings -v` and verify failures.
3. Implement in-memory quota counters, usage ledger, and deterministic embeddings.
4. Run the tests and verify they pass.

### Task 5: Full Verification

**Files:**
- Modify as needed based on failures.

**Steps:**
1. Run `python3 -m unittest discover -v`.
2. Run a syntax compile check with `python3 -m compileall mixapi tests`.
3. Fix failures with test-first changes where behavior is missing.
4. Record final status.

