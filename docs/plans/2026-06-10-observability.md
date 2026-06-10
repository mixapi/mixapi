# Observability Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Record provider latency, provider errors, fallbacks, circuit state, budget denials, and request-correlated trace spans across MixAPI dispatch paths.

**Architecture:** Add a thread-safe, dependency-free collector behind a small observability protocol and inject it through `create_app`. Instrument existing routing and dispatch helpers without changing public response shapes, and retain low-cardinality metadata only.

**Tech Stack:** Python 3.14, FastAPI, dataclasses, `time.monotonic`, `threading.Lock`, unittest.

### Task 1: Collector primitives

**Files:**
- Create: `mixapi/observability.py`
- Create: `tests/test_observability.py`

1. Write failing tests for counter aggregation, histogram observations, latest gauge values, and completed spans.
2. Run `python3 -m unittest tests.test_observability.ObservabilityCollectorTest -v` and verify import or API failures.
3. Implement immutable records and a thread-safe `InMemoryObservability` collector with deterministic snapshot methods.
4. Re-run the collector tests and verify they pass.

### Task 2: Provider attempts and fallback telemetry

**Files:**
- Modify: `mixapi/app.py`
- Modify: `tests/test_observability.py`

1. Add failing endpoint tests for successful provider latency spans, provider failure counters, and fallback counters.
2. Run the focused tests and verify the expected metrics and spans are absent.
3. Inject the collector through `create_app`, time response, embedding, and stream initialization attempts, and emit one fallback counter after candidate selection.
4. Re-run focused tests and the existing fallback and streaming modules.

### Task 3: Circuit and budget telemetry

**Files:**
- Modify: `mixapi/app.py`
- Modify: `tests/test_observability.py`

1. Add failing tests for open and closed circuit gauges plus request-level and API-key budget denials.
2. Run the focused tests and verify they fail because those signals are absent.
3. Emit circuit gauges during filtering and state transitions. Wrap both budget eligibility and reservation paths to emit denial counters and `budget.reserve` spans.
4. Re-run focused tests and existing circuit and budget modules.

### Task 4: Documentation and verification

**Files:**
- Modify: `TASKS.md`

1. Run `python3 -m unittest tests.test_observability tests.test_fallbacks tests.test_streaming tests.test_circuit_breakers tests.test_budgets -v`.
2. Run `python3 -m unittest discover -s tests -v`.
3. Run `python3 -m compileall mixapi tests` and `git diff --check`.
4. Mark the observability backlog item complete only after all checks pass.
5. Review the final diff and commit the implementation.
