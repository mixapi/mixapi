# Gemini Native Adapter Design

## Scope

Add a configurable Gemini native `generateContent` adapter for `/v1/responses`. The adapter supports text conversations, leading system instructions, function declarations, tool-choice controls, JSON Schema response hints, output-token limits, normalized text, normalized function calls, usage reconciliation, and existing fallback behavior.

Remote image ingestion, uploaded Gemini Files, and multi-turn tool-result replay are out of scope. Those require a provider-neutral file lifecycle and preservation of Gemini continuation metadata that the current public contract does not expose.

## Architecture

`create_app` configures `GeminiProviderAdapter` from explicit arguments or `MIXAPI_GEMINI_BASE_URL` and `MIXAPI_GEMINI_API_KEY`. The existing composite adapter selects it only for Gemini candidates.

The adapter sends `POST /v1beta/models/{provider_model}:generateContent` with `x-goog-api-key`. Portable user and assistant messages become Gemini `contents` with `parts`; leading system messages become `systemInstruction`. Portable tools become one `functionDeclarations` collection. Tool choices map to `AUTO`, `ANY`, or `NONE`, optionally with `allowedFunctionNames`. Structured output and output limits merge into `generationConfig`, while portable fields override conflicting native provider options.

## Response And Errors

Text parts are concatenated into `output_text`. Gemini `functionCall` parts become MixAPI `function_call` output items. Because the native response has no call ID, MixAPI creates a deterministic ID from the call index, name, and canonical JSON arguments. Usage reads `promptTokenCount` and `candidatesTokenCount` from `usageMetadata`.

Transport, status, JSON, candidate, content, function-call, and usage failures map to `ProviderDispatchError`, preserving route attempts and fallback behavior.

## Verification

Contract tests use a local fake HTTP server. They verify path encoding, API-key header, request translation, structured output, function-call normalization, usage, explicit configuration, and provider failures. Full tests, compilation, whitespace checks, and a live local HTTP smoke test close the increment.
