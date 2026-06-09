# Persistent Budget and Circuit State Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Persist spend reservations and provider circuit state so multiple MixAPI processes sharing one SQLite database enforce the same limits and routing health.

**Architecture:** Add SQLite implementations behind the existing budget and circuit method contracts. Budget reserve, reconcile, and release operations use `BEGIN IMMEDIATE` transactions and unique reservation IDs so concurrent processes cannot oversubscribe a key or reconcile the wrong reservation. Circuit state stores wall-clock open timestamps and applies each read/update transactionally so processes observe failures, successes, and recovery consistently.

**Tech Stack:** Python 3, SQLite via `sqlite3`, FastAPI application wiring, `unittest`.

### Task 1: Persistent budget behavior

**Files:**
- Modify: `mixapi/budget.py`
- Modify: `mixapi/persistence.py`
- Modify: `tests/test_persistence.py`

**Step 1: Write failing tests**

Add tests proving that two application instances sharing one database cannot reserve beyond the combined API-key budget, that committed spend survives app recreation, and that release removes only the identified reservation.

**Step 2: Run the focused tests and verify failure**

Run: `python3 -m unittest tests.test_persistence.PersistenceTest.test_budget_state_is_shared_between_app_instances tests.test_persistence.PersistenceTest.test_budget_reservation_release_is_persistent`

Expected: FAIL because `create_app(database_path=...)` still uses `InMemoryBudgetService`.

**Step 3: Implement the minimal persistent budget service**

Add a unique ID to `BudgetReservation`. Add `budget_spend` and `budget_reservations` tables. Implement `SQLiteBudgetService.reserve`, `.reconcile`, and `.release` with `BEGIN IMMEDIATE`; reject a reservation when committed plus all outstanding reservations plus the estimate exceeds the configured limit.

**Step 4: Run the focused tests**

Run: `python3 -m unittest tests.test_persistence.PersistenceTest.test_budget_state_is_shared_between_app_instances tests.test_persistence.PersistenceTest.test_budget_reservation_release_is_persistent`

Expected: PASS.

### Task 2: Persistent circuit behavior

**Files:**
- Modify: `mixapi/circuits.py`
- Modify: `mixapi/persistence.py`
- Modify: `tests/test_persistence.py`

**Step 1: Write failing tests**

Add tests proving that a transient failure recorded through one service instance opens the circuit for another, that success clears shared state, and that an expired open circuit is cleared for every process.

**Step 2: Run the focused tests and verify failure**

Run: `python3 -m unittest tests.test_persistence.PersistenceTest.test_circuit_state_is_shared_between_app_instances tests.test_persistence.PersistenceTest.test_persistent_circuit_recovers_after_timeout`

Expected: FAIL because circuit state is process-local.

**Step 3: Implement the minimal persistent circuit breaker**

Add a `circuit_states` table keyed by provider and provider model. Implement `SQLiteCircuitBreaker` using wall-clock seconds, transactional failure increments, success deletion, and compare-and-delete recovery after the configured timeout.

**Step 4: Run the focused tests**

Run the same focused test command and expect PASS.

### Task 3: Application wiring and regression coverage

**Files:**
- Modify: `mixapi/app.py`
- Modify: `tests/test_persistence.py`
- Modify: `TASKS.md`

**Step 1: Wire persistent services**

When `database_path` or `MIXAPI_DATABASE_PATH` is configured, construct `SQLiteBudgetService` and `SQLiteCircuitBreaker` from the shared `SQLiteDatabase`. Preserve in-memory services when persistence is not configured.

**Step 2: Run focused persistence and existing behavior tests**

Run: `python3 -m unittest tests.test_persistence tests.test_budgets tests.test_circuit_breakers`

Expected: PASS.

**Step 3: Run full verification**

Run: `python3 -m unittest discover -s tests`

Run: `python3 -m compileall -q mixapi tests`

Run: `rg -n "[[:blank:]]+$" mixapi tests TASKS.md docs/plans/2026-06-09-persistent-budget-circuits.md`

Expected: all tests pass, compilation succeeds, and the whitespace scan has no matches.

**Step 4: Update the task queue**

Mark `Persist budget reservations and circuit state for multi-process deployments` complete only after all verification commands pass.

**Step 5: Commit**

Committing is unavailable until the repository has an initial `HEAD`; leave the verified changes in the shared workspace without altering unrelated files.
