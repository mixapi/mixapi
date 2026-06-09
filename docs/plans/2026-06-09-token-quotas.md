# Token Quotas Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add cumulative per-API-key token quotas alongside the existing request quota, with pre-dispatch reservation and actual-usage reconciliation.

**Architecture:** Extend the in-memory quota service with unique token reservations and atomic counters for committed and outstanding tokens. The application estimates the largest possible token use across eligible fallback candidates, reserves that amount before dispatch, then reconciles actual input plus output tokens on success or interrupted streaming; failures before billable work release the reservation.

**Tech Stack:** Python 3.14, FastAPI, dataclasses, threading locks, unittest.

### Task 1: Specify Quota Service Behavior

**Files:**
- Modify: `tests/test_quota_usage_embeddings.py`
- Modify: `tests/test_streaming.py`
- Test: `tests/test_quota_usage_embeddings.py`
- Test: `tests/test_streaming.py`

1. Add a test proving estimated response tokens are denied before provider dispatch when the limit is too small.
2. Add a test proving successful response usage is reconciled to actual tokens rather than retaining the estimate.
3. Add tests proving embeddings consume tokens and pre-dispatch provider failures release reservations.
4. Extend the interrupted-stream test to prove partial actual usage is reconciled.
5. Run `python3 -m unittest tests.test_quota_usage_embeddings tests.test_streaming` and confirm the new tests fail for missing token-quota behavior.

### Task 2: Implement Atomic Token Reservations

**Files:**
- Modify: `mixapi/quota.py`
- Test: `tests/test_quota_usage_embeddings.py`

1. Add an immutable `TokenReservation` carrying the API-key ID, estimated amount, and unique reservation ID.
2. Add a `QuotaService` protocol for request reservation and token reserve/reconcile/release operations.
3. Track committed tokens and active reservations under a lock so concurrent requests cannot oversubscribe the in-memory limit.
4. Make reconciliation and release idempotent by removing reservations by ID.
5. Run the focused quota tests and confirm service-level behavior passes.

### Task 3: Integrate Token Quotas Into Dispatch Lifecycles

**Files:**
- Modify: `mixapi/app.py`
- Test: `tests/test_quota_usage_embeddings.py`
- Test: `tests/test_streaming.py`

1. Add `token_quota_limit` to `create_app`, with `MIXAPI_TOKEN_QUOTA_LIMIT` as the environment fallback.
2. Estimate input tokens plus the maximum output cap across eligible response candidates; estimate embeddings from input only.
3. Reserve tokens before consuming the request quota, releasing budget and token reservations if either gate denies the request.
4. Reconcile actual tokens for non-streaming responses, embeddings, completed streams, and interrupted streams.
5. Release token reservations when all provider attempts fail before a response or stream begins.
6. Expose the quota service through `app.state.quota` for operational inspection and focused tests.
7. Run `python3 -m unittest tests.test_quota_usage_embeddings tests.test_streaming` and confirm all focused tests pass.

### Task 4: Verify And Close The Task

**Files:**
- Modify: `TASKS.md`

1. Run `python3 -m unittest discover -s tests`.
2. Run `python3 -m compileall -q mixapi tests`.
3. Scan changed files for trailing whitespace.
4. Mark the token-quota task complete only after all checks pass.
5. Review the diff for quota ordering, reservation leaks, and streaming finalization regressions.

Git commits and worktree creation are omitted because this repository has no valid `HEAD`; all files are currently untracked.
