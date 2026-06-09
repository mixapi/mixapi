# Usage Reporting Filters, Pagination, and JSONL Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add tenant-scoped time-window filters, stable cursor pagination, and JSONL export to usage reporting without breaking the existing usage and CSV contracts.

**Architecture:** Add an immutable UTC `created_at` timestamp and internal append-order identifier to usage records. Both ledgers expose a shared query method that applies tenant and inclusive time bounds, orders oldest-first, and returns one extra record to determine `has_more`; opaque cursors encode the last append-order identifier and the active filter window so they cannot be reused with different filters or tenants. The API validates reporting query parameters once and uses the same ledger filters for JSON, CSV, and JSONL output.

**Tech Stack:** Python 3.14, FastAPI, SQLite, `unittest`, standard-library `datetime`, `base64`, `json`, and `csv`.

### Task 1: Define Timestamped Usage Records and Query Results

**Files:**
- Modify: `mixapi/usage.py`
- Test: `tests/test_usage_endpoint.py`

1. Add failing tests proving usage events expose RFC 3339 UTC `created_at` values and can be filtered with inclusive `start_time` and `end_time` query parameters.
2. Run `python3 -m unittest tests.test_usage_endpoint -v` and confirm the tests fail because timestamps and filters are absent.
3. Add `created_at`, `sequence_id`, `UsageQuery`, and `UsagePage` primitives plus strict timestamp parsing and query validation.
4. Add an in-memory ledger `query()` implementation while preserving the existing `events()` API.
5. Run the focused tests and confirm they pass.

### Task 2: Add Stable Cursor Pagination

**Files:**
- Modify: `mixapi/usage.py`
- Modify: `mixapi/app.py`
- Test: `tests/test_usage_endpoint.py`

1. Add failing tests for `limit`, `has_more`, `next_cursor`, page traversal without duplicates, tenant isolation, and invalid limit/cursor responses.
2. Run the focused tests and confirm failures are caused by missing pagination behavior.
3. Implement URL-safe opaque cursor encoding and decoding. Bind cursors to tenant, start time, and end time, and reject malformed or mismatched cursors with normalized validation errors.
4. Extend `GET /v1/usage` to return page metadata while retaining `object`, `data`, and page-level `summary` fields.
5. Run the focused tests and confirm they pass.

### Task 3: Persist and Query Timestamps in SQLite

**Files:**
- Modify: `mixapi/persistence.py`
- Test: `tests/test_persistence.py`

1. Add failing persistence tests for timestamp round-tripping and paginated time-window queries across application recreation.
2. Run `python3 -m unittest tests.test_persistence -v` and confirm the tests fail for missing schema/query support.
3. Add a backward-compatible `created_at` schema migration, persist timestamps, use SQLite row IDs as sequence IDs, and implement indexed tenant/time/cursor queries.
4. Run persistence and usage endpoint tests and confirm in-memory/SQLite parity.

### Task 4: Add Filtered CSV and JSONL Export

**Files:**
- Modify: `mixapi/usage.py`
- Modify: `mixapi/app.py`
- Test: `tests/test_usage_endpoint.py`

1. Add failing tests proving CSV honors time filters and JSONL returns one public event per line with `application/x-ndjson`, tenant scoping, and a download filename.
2. Run the focused tests and confirm JSONL is rejected by the current implementation.
3. Add `usage_jsonl()` and allow `format=csv|jsonl`; reject all other formats through the existing normalized error envelope.
4. Run focused tests and confirm they pass.

### Task 5: Verify and Close the Backlog Item

**Files:**
- Modify: `TASKS.md`

1. Run `python3 -m unittest discover -s tests`.
2. Run `python3 -m compileall -q mixapi tests`.
3. Run a whitespace scan on changed files.
4. Review the diff for tenant isolation, cursor stability, schema migration compatibility, and public contract regressions.
5. Mark the usage reporting follow-up complete only after all checks pass.

