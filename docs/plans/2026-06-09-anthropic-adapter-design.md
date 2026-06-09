# Anthropic Provider Adapter Design

## Scope

Add a real, configurable Anthropic Messages API adapter for `/v1/responses`. The adapter supports text conversations, URL image content, client-defined function tools, named or automatic tool choice, normalized text output, normalized function calls, usage reconciliation, and the existing provider failure path. Embeddings and provider-native streaming are out of scope because Anthropic is not an embedding candidate and MixAPI currently buffers all response adapters before emitting normalized SSE.

## Architecture

`create_app` configures an `AnthropicProviderAdapter` when `MIXAPI_ANTHROPIC_BASE_URL` is set or an explicit base URL is supplied. The existing `CompositeProviderAdapter` selects it only for Anthropic candidates; unconfigured providers continue using the deterministic adapter.

The adapter owns all Anthropic-specific behavior. It translates the portable request into `POST /v1/messages`, supplies `x-api-key` and `anthropic-version`, maps leading system instructions to the top-level `system` field, converts image URLs to Anthropic URL source blocks, and converts portable tools to `name`, `description`, and `input_schema`. Portable `tool_choice` values map to Anthropic `auto`, `none`, `any`, or named `tool` forms.

## Response And Errors

Anthropic `text` blocks are concatenated into `output_text`. Each `tool_use` block becomes a normalized MixAPI `function_call` item whose arguments are compact JSON. Usage reads `input_tokens` and `output_tokens` from the Anthropic response.

Timeouts, network failures, non-success status codes, invalid JSON, and malformed content become `ProviderDispatchError`, preserving the current fallback and route-decision behavior. Portable translated fields override conflicting native provider options, while unrelated native options pass through.

## Verification

Contract tests use a local `ThreadingHTTPServer` fake to verify path, headers, request translation, tool-call normalization, and failures. The full unit suite, bytecode compilation, whitespace checks, and one live HTTP smoke request complete the increment.
