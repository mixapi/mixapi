# Ollama Native Adapter Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Route configured Ollama candidates through native chat and embedding APIs.

**Architecture:** Add an Ollama-specific adapter behind `CompositeProviderAdapter`. Translate portable chat/format/options fields into `/api/chat`, translate embeddings into `/api/embed`, normalize responses into `AdapterResponse`, and reuse existing routing, fallback, usage, and persistence behavior.

**Tech Stack:** Python 3.14, FastAPI, httpx, standard-library `unittest`, local `ThreadingHTTPServer` fake upstream.

### Task 1: Native Chat Dispatch

**Files:**
- Create: `tests/test_ollama_adapter.py`
- Modify: `mixapi/adapters.py`
- Modify: `mixapi/app.py`

**Steps:**
1. Write a failing pinned-provider test for `/api/chat`, authorization, model, messages, text output, and token usage.
2. Run the test and confirm deterministic dispatch is still used.
3. Implement explicit/environment configuration and basic chat dispatch.
4. Run the focused test and verify it passes.

### Task 2: Chat Options And Structured Format

**Files:**
- Modify: `tests/test_ollama_adapter.py`
- Modify: `mixapi/adapters.py`

**Steps:**
1. Write a failing test for system messages, native options, portable output limit, and JSON response mode.
2. Run the test and verify translated fields are missing.
3. Implement option merging and format translation with portable precedence.
4. Run focused tests and verify they pass.

### Task 3: Native Embeddings

**Files:**
- Modify: `tests/test_ollama_adapter.py`
- Modify: `mixapi/adapters.py`

**Steps:**
1. Write a failing test for `/api/embed`, model/input translation, normalized vector, and usage.
2. Run the test and verify deterministic embeddings are returned.
3. Implement native embedding dispatch and parsing.
4. Run focused tests and verify they pass.

### Task 4: Failure Hardening And Verification

**Files:**
- Modify: `tests/test_ollama_adapter.py`

**Steps:**
1. Write tests for `/api`-prefixed base URLs, malformed chat output, and 5xx route attempts.
2. Complete failure handling until focused tests pass.
3. Run `python3 -m unittest discover -v`.
4. Run `python3 -m compileall mixapi tests`.
5. Run `git diff --check` and trailing-whitespace checks.
6. Smoke-test configured Ollama chat and embedding requests against a local fake API.
