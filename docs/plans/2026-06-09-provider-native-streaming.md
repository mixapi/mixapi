# Provider-Native Streaming Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace buffered response streaming with provider-native streams while allowing fallback only before the first client-visible event.

**Architecture:** Each response adapter exposes `start_response_stream`, which opens the provider connection and primes it through the first normalized text delta or terminal provider frame. A `ProviderStream` owns the remaining iterator, accumulated text, and final usage. The API endpoint attempts candidates synchronously until one stream is successfully primed, then emits normalized SSE events without switching providers after output begins. Completion or interruption finalizes budget, usage, circuit, and route state exactly once.

**Tech Stack:** Python 3.14, FastAPI `StreamingResponse`, synchronous `httpx.stream`, standard-library JSON/SSE parsing, `unittest`.

### Task 1: Define the stream contract and normalization helpers

**Files:**
- Modify: `mixapi/adapters.py`
- Modify: `mixapi/streaming.py`
- Test: `tests/test_streaming.py`

**Steps:**
1. Add failing tests for a primed stream, ordered normalized deltas, final usage, and post-start interruption.
2. Run `python3 -m unittest tests.test_streaming -v` and confirm the new assertions fail.
3. Add `ProviderStream`, `ProviderStreamEvent`, and normalized SSE generation helpers.
4. Re-run the focused tests and confirm the contract tests pass.

### Task 2: Add provider-native stream translators

**Files:**
- Modify: `mixapi/adapters.py`
- Test: `tests/test_openai_compatible_adapter.py`
- Test: `tests/test_anthropic_adapter.py`
- Test: `tests/test_gemini_adapter.py`
- Test: `tests/test_ollama_adapter.py`

**Steps:**
1. Add fake-provider streaming fixtures and failing tests that verify each outbound request enables streaming.
2. Verify OpenAI SSE deltas and usage frames normalize correctly.
3. Verify Anthropic SSE content deltas and message usage normalize correctly.
4. Verify Gemini streamed JSON/SSE candidates and usage metadata normalize correctly.
5. Verify Ollama newline-delimited JSON messages and terminal counts normalize correctly.
6. Run each adapter test module after its minimal translator is implemented.

### Task 3: Orchestrate streaming fallback and finalization

**Files:**
- Modify: `mixapi/app.py`
- Modify: `mixapi/streaming.py`
- Test: `tests/test_streaming.py`
- Test: `tests/test_fallbacks.py`

**Steps:**
1. Add a failing test proving a provider failure during stream priming falls back before `response.created`.
2. Add a failing test proving failure after the first emitted delta produces `response.failed` with `stream_interrupted` and does not call another provider.
3. Add a streaming dispatch path that reserves quota/budget before dispatch and primes each candidate.
4. Emit `response.created`, deltas, and `response.completed` only from the selected native stream.
5. Reconcile final usage and cost, persist one route decision and one usage event, and update circuit state.
6. On interruption, persist the partial route and usage state and emit a terminal failure event without fallback.

### Task 4: Complete verification and backlog tracking

**Files:**
- Modify: `TASKS.md`

**Steps:**
1. Run `python3 -m unittest tests.test_streaming -v`.
2. Run all provider adapter test modules.
3. Run `python3 -m unittest discover -s tests`.
4. Run `python3 -m compileall -q mixapi tests`.
5. Scan edited files for trailing whitespace.
6. Mark provider-native streaming complete only after all checks pass.

## Behavioral Decisions

- A stream is not client-visible until the selected provider has yielded its first usable delta or a valid terminal frame.
- Provider fallback is allowed during stream open and priming failures only.
- Once `response.created` is emitted, any provider error terminates the stream with `response.failed`; no second provider is attempted.
- Client disconnects close the provider stream and reconcile partial usage without attempting fallback.
- The gateway preserves provider delta order and does not re-chunk provider text.
- The completed response uses accumulated deltas and provider-reported usage when available, falling back to local token estimates only when the provider omits usage.
- Tool-call streaming remains outside this task; requests combining `stream: true` with tools are rejected explicitly, while non-streaming tool behavior is unchanged.
