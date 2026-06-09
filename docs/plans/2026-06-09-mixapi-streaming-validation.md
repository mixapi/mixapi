# MixAPI Streaming And Validation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add normalized Server-Sent Event streaming and stronger request validation to the MixAPI MVP.

**Architecture:** Keep provider dispatch deterministic and synchronous, then expose the normalized result as an ordered SSE event sequence. Extract request validation into a focused module so both streaming and non-streaming paths reject malformed input before quota reservation, routing, or usage recording.

**Tech Stack:** Python 3.14, FastAPI, Pydantic v2, standard-library `unittest`, FastAPI `TestClient`.

### Task 1: Request Validation

**Files:**
- Create: `mixapi/validation.py`
- Modify: `mixapi/app.py`
- Test: `tests/test_request_validation.py`

**Steps:**
1. Write failing tests for empty response input, empty embedding input, invalid stream values, and streaming with an idempotency key.
2. Run `python3 -m unittest tests.test_request_validation -v` and verify failures.
3. Implement shared endpoint validation before route planning and quota reservation.
4. Run the focused tests and verify they pass.

### Task 2: Normalized SSE Streaming

**Files:**
- Create: `mixapi/streaming.py`
- Modify: `mixapi/app.py`
- Test: `tests/test_streaming.py`

**Steps:**
1. Write failing tests for SSE content type, ordered events, route metadata, and usage persistence.
2. Run `python3 -m unittest tests.test_streaming -v` and verify failures.
3. Implement normalized `response.created`, `response.output_text.delta`, and `response.completed` events.
4. Return `StreamingResponse` when `stream` is true.
5. Run the focused tests and verify they pass.

### Task 3: Streaming Fallback Semantics

**Files:**
- Modify: `mixapi/app.py`
- Test: `tests/test_streaming.py`

**Steps:**
1. Write a failing test showing fallback occurs before the first streamed event.
2. Run the focused test and verify failure.
3. Reuse pre-stream fallback execution and persist all attempts before yielding events.
4. Run the focused tests and verify they pass.

### Task 4: Full Verification

**Steps:**
1. Run `python3 -m unittest discover -v`.
2. Run `python3 -m compileall mixapi tests`.
3. Run `git diff --check`.
4. Start the API and smoke-test an SSE request.

