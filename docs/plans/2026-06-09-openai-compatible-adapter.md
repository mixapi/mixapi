# OpenAI-Compatible Provider Adapter Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Route configured OpenAI provider calls to a real OpenAI-compatible HTTP backend for chat responses and embeddings.

**Architecture:** Add an HTTP adapter that translates MixAPI requests into OpenAI-compatible `/v1/chat/completions` and `/v1/embeddings` requests. Compose it with the deterministic adapter so unconfigured providers remain local, and convert upstream transport or status failures into the existing fallback mechanism.

**Tech Stack:** Python 3.14, FastAPI, httpx, standard-library `unittest`, local `ThreadingHTTPServer` fake upstream.

### Task 1: Chat Completions HTTP Adapter

**Files:**
- Modify: `pyproject.toml`
- Modify: `mixapi/adapters.py`
- Modify: `mixapi/app.py`
- Test: `tests/test_openai_compatible_adapter.py`

**Steps:**
1. Write a failing integration test that pins the OpenAI provider and verifies upstream path, authorization, model, messages, response text, and usage.
2. Run the focused test and verify failure.
3. Implement `OpenAICompatibleProviderAdapter` and composite adapter selection.
4. Configure it through app arguments and `MIXAPI_OPENAI_BASE_URL` / `MIXAPI_OPENAI_API_KEY`.
5. Run the focused test and verify it passes.

### Task 2: Embeddings HTTP Adapter

**Files:**
- Modify: `mixapi/adapters.py`
- Test: `tests/test_openai_compatible_adapter.py`

**Steps:**
1. Write a failing test for `/v1/embeddings` translation and normalized usage.
2. Run the focused test and verify failure.
3. Implement embeddings dispatch and response translation.
4. Run the focused tests and verify they pass.

### Task 3: Upstream Failure And Fallback

**Files:**
- Modify: `mixapi/adapters.py`
- Test: `tests/test_openai_compatible_adapter.py`

**Steps:**
1. Write failing tests for upstream 5xx fallback and invalid upstream payload handling.
2. Run the focused tests and verify failures.
3. Map timeout, network, non-2xx, and malformed response failures to `ProviderDispatchError`.
4. Run the focused tests and verify they pass.

### Task 4: Full Verification

**Steps:**
1. Run `python3 -m unittest discover -v`.
2. Run `python3 -m compileall mixapi tests`.
3. Run `git diff --check`.
4. Start MixAPI against a local fake OpenAI-compatible server and smoke-test one request.

