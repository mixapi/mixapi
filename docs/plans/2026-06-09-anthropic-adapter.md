# Anthropic Provider Adapter Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Route configured Anthropic candidates through the Messages API with portable message and tool translation.

**Architecture:** Add an Anthropic-specific HTTP adapter behind the existing composite adapter. Translate MixAPI requests at the adapter boundary, normalize text and tool-use content blocks into `AdapterResponse`, and reuse the current fallback, usage, and route-decision paths.

**Tech Stack:** Python 3.14, FastAPI, httpx, standard-library `unittest`, local `ThreadingHTTPServer` fake upstream.

### Task 1: Basic Messages API Dispatch

**Files:**
- Create: `tests/test_anthropic_adapter.py`
- Modify: `mixapi/adapters.py`
- Modify: `mixapi/app.py`

**Steps:**
1. Write a failing integration test that pins Anthropic and verifies `/v1/messages`, `x-api-key`, `anthropic-version`, model, messages, `max_tokens`, normalized text, and usage.
2. Run the focused test and verify it fails because no Anthropic HTTP adapter is configured.
3. Implement adapter configuration, HTTP dispatch, text response parsing, and usage parsing.
4. Run the focused test and verify it passes.

### Task 2: Portable Content Translation

**Files:**
- Modify: `tests/test_anthropic_adapter.py`
- Modify: `mixapi/adapters.py`

**Steps:**
1. Write a failing test for a leading system message, URL image content, portable maximum output tokens, and an unrelated native option.
2. Run the focused test and verify the translated payload is incomplete.
3. Implement top-level system extraction, Anthropic text/image blocks, portable maximum-token precedence, and native-option preservation.
4. Run the focused tests and verify they pass.

### Task 3: Tools And Tool-Use Normalization

**Files:**
- Modify: `tests/test_anthropic_adapter.py`
- Modify: `mixapi/adapters.py`

**Steps:**
1. Write a failing test that verifies portable tool and named tool-choice translation and normalizes an Anthropic `tool_use` response block.
2. Run the focused test and verify it fails.
3. Implement tool request translation, tool-choice mapping, and tool-use response parsing.
4. Run the focused tests and verify they pass.

### Task 4: Failure Mapping And Full Verification

**Files:**
- Modify: `tests/test_anthropic_adapter.py`

**Steps:**
1. Write a failing test that verifies an Anthropic 5xx is recorded through the existing provider failure path.
2. Run the focused test and verify it fails if failure mapping is incomplete.
3. Complete timeout, network, status, and malformed-response mapping.
4. Run `python3 -m unittest discover -v`.
5. Run `python3 -m compileall mixapi tests`.
6. Run `git diff --check` and scan source files for trailing whitespace.
7. Smoke-test a pinned Anthropic tool request against a local fake Messages API.
