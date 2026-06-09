# MixAPI SQLite Persistence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Persist usage, idempotency records, and route decisions across application restarts and expose tenant-scoped usage data.

**Architecture:** Add SQLite-backed implementations with the same public methods as the existing in-memory stores. Keep in-memory stores as the default for isolated tests, and select SQLite through `create_app(database_path=...)` or `MIXAPI_DATABASE_PATH` for a durable local deployment.

**Tech Stack:** Python 3.14, standard-library `sqlite3`, FastAPI, standard-library `unittest`, FastAPI `TestClient`.

### Task 1: Persistent Usage Ledger

**Files:**
- Create: `mixapi/persistence.py`
- Modify: `mixapi/usage.py`
- Modify: `mixapi/app.py`
- Test: `tests/test_persistence.py`

**Steps:**
1. Write a failing test proving usage events survive application recreation with the same SQLite path.
2. Run `python3 -m unittest tests.test_persistence -v` and verify failure.
3. Add schema initialization and a SQLite usage ledger.
4. Select persistent stores when a database path is configured.
5. Run the focused test and verify it passes.

### Task 2: Persistent Idempotency And Route Decisions

**Files:**
- Modify: `mixapi/persistence.py`
- Modify: `mixapi/app.py`
- Test: `tests/test_persistence.py`

**Steps:**
1. Write failing tests for idempotency replay and route decision lookup after application recreation.
2. Run the focused tests and verify failures.
3. Add SQLite idempotency and route-decision stores.
4. Run the focused tests and verify they pass.

### Task 3: Usage API

**Files:**
- Modify: `mixapi/usage.py`
- Modify: `mixapi/app.py`
- Test: `tests/test_usage_endpoint.py`

**Steps:**
1. Write failing tests for tenant-scoped event listing and aggregate totals.
2. Run `python3 -m unittest tests.test_usage_endpoint -v` and verify failures.
3. Add `GET /v1/usage` and public event serialization.
4. Run the focused tests and verify they pass.

### Task 4: Full Verification

**Steps:**
1. Run `python3 -m unittest discover -v`.
2. Run `python3 -m compileall mixapi tests`.
3. Run `git diff --check`.
4. Start the API with `MIXAPI_DATABASE_PATH` and smoke-test persistence across restart.

