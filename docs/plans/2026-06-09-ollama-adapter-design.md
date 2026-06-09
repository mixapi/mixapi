# Ollama Native Adapter Design

## Scope

Add a configurable native Ollama adapter for chat responses and embeddings. The adapter supports portable text conversations, leading system messages, output-token limits, JSON response mode, native runtime options, normalized text, usage accounting, and the existing fallback path.

Function tools remain unsupported for the cataloged `llama3.1` candidate. Ollama supports tool calling generally, but MixAPI capability records are model-specific and must not promise a feature without an explicit model profile. Image input and native streaming are also out of scope for this increment.

## Architecture

`create_app` configures `OllamaProviderAdapter` from explicit arguments or `MIXAPI_OLLAMA_BASE_URL` and `MIXAPI_OLLAMA_API_KEY`. The existing composite adapter selects it for Ollama chat and embedding candidates; without configuration, deterministic local behavior remains available for tests and development.

Chat requests use `POST /api/chat` with `stream: false`. Portable messages become Ollama messages. `max_output_tokens` maps to `options.num_predict`. JSON object requests map to `format: "json"`. Native provider options are preserved, with portable fields winning conflicts. Strict JSON Schema remains unavailable for the current `llama3.1` capability profile.

Embedding requests use `POST /api/embed`, preserve native options, and normalize the first returned embedding.

## Response And Errors

Chat output reads `message.content`; usage reads `prompt_eval_count` and `eval_count`. Embedding usage reads `prompt_eval_count`. An optional API key is sent as a bearer token for Ollama cloud.

Timeouts, network failures, non-success statuses, invalid JSON, malformed chat messages, missing embeddings, and invalid usage map to `ProviderDispatchError`, preserving route attempts and fallback behavior.

## Verification

Local fake-server tests verify chat, system/options/format translation, embeddings, explicit and environment configuration, base URLs ending in `/api`, malformed responses, and 5xx failures. Full tests, compilation, whitespace checks, and a live local smoke request close the increment.
