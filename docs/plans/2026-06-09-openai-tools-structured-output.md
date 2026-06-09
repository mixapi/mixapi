# OpenAI Tools And Structured Output Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Support portable function tools and JSON-schema output through configured OpenAI-compatible chat backends.

**Architecture:** Extend routing to require function-tool capability when tools are present. Translate MixAPI's portable tool, tool-choice, and response-format fields at the HTTP adapter boundary, then normalize upstream chat tool calls into MixAPI response output items without changing deterministic providers.

**Tech Stack:** Python 3.14, FastAPI, httpx, standard-library `unittest`, local `ThreadingHTTPServer` fake upstream.

### Task 1: Tool-Aware Routing

**Files:**
- Modify: `mixapi/routing.py`
- Test: `tests/test_responses_endpoint.py`

**Steps:**
1. Write a failing endpoint test showing a lowest-cost tool request rejects the local candidate and selects a function-capable provider.
2. Run the focused test and verify failure.
3. Add function-tool capability detection and candidate rejection.
4. Run the focused test and verify it passes.

### Task 2: OpenAI Request Translation

**Files:**
- Modify: `mixapi/adapters.py`
- Test: `tests/test_openai_compatible_adapter.py`

**Steps:**
1. Write a failing integration test for portable tools, named tool choice, and JSON-schema response format.
2. Run the focused test and verify failure.
3. Translate portable fields to OpenAI-compatible chat-completions fields while preserving native provider options.
4. Run the focused tests and verify they pass.

### Task 3: Tool-Call Response Normalization

**Files:**
- Modify: `mixapi/adapters.py`
- Modify: `mixapi/app.py`
- Modify: `mixapi/streaming.py`
- Test: `tests/test_openai_compatible_adapter.py`

**Steps:**
1. Write a failing integration test for an upstream assistant message containing a function tool call and no text.
2. Run the focused test and verify failure.
3. Parse upstream tool calls, expose normalized `function_call` output items, and avoid empty streaming text deltas.
4. Run focused and full tests.

### Task 4: Full Verification

**Steps:**
1. Run `python3 -m unittest discover -v`.
2. Run `python3 -m compileall mixapi tests`.
3. Run `git diff --check`.
4. Smoke-test a tool request against a local fake OpenAI-compatible server.
