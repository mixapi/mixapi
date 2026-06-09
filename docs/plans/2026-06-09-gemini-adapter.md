# Gemini Native Adapter Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Route configured Gemini candidates through the native `generateContent` API.

**Architecture:** Add a Gemini-specific HTTP adapter behind `CompositeProviderAdapter`. Translate portable request fields into Gemini `contents`, tools, tool configuration, and generation configuration, then normalize text/function-call parts and usage into `AdapterResponse`.

**Tech Stack:** Python 3.14, FastAPI, httpx, standard-library `unittest`, local `ThreadingHTTPServer` fake upstream.

### Task 1: Basic GenerateContent Dispatch

**Files:**
- Create: `tests/test_gemini_adapter.py`
- Modify: `mixapi/adapters.py`
- Modify: `mixapi/app.py`

**Steps:**
1. Write a failing test that pins Gemini and verifies path, API-key header, model content, normalized text, and usage.
2. Run the focused test and confirm it uses the deterministic adapter.
3. Implement explicit/environment configuration and basic Gemini dispatch/response parsing.
4. Run the focused test and verify it passes.

### Task 2: System And Generation Configuration

**Files:**
- Modify: `tests/test_gemini_adapter.py`
- Modify: `mixapi/adapters.py`

**Steps:**
1. Write a failing test for system instruction, role translation, `max_output_tokens`, JSON Schema response format, and unrelated native generation configuration.
2. Run the focused test and verify translation fields are missing.
3. Implement portable content and `generationConfig` merging with portable precedence.
4. Run focused tests and verify they pass.

### Task 3: Function Calling

**Files:**
- Modify: `tests/test_gemini_adapter.py`
- Modify: `mixapi/adapters.py`

**Steps:**
1. Write a failing test for function declarations, named tool choice, and `functionCall` normalization.
2. Run the focused test and verify it fails.
3. Implement tool declaration/config translation and deterministic call IDs.
4. Run focused tests and verify they pass.

### Task 4: Failure Mapping And Verification

**Files:**
- Modify: `tests/test_gemini_adapter.py`

**Steps:**
1. Write failing tests for malformed content and upstream 5xx route attempts.
2. Complete failure mapping until focused tests pass.
3. Run `python3 -m unittest discover -v`.
4. Run `python3 -m compileall mixapi tests`.
5. Run `git diff --check` and trailing-whitespace checks.
6. Smoke-test a pinned Gemini tool request against a local fake API.
