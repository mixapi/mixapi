# Detailed Technical Design: MixAPI Unified LLM Gateway

## Source

This design is derived from `deep-research-report.md` and `product-solutions-prd.md`. It turns the PRD into an implementation-facing architecture for the MVP and near-term expansion.

## Status

Draft technical design. The repository does not yet contain application code, so this document is language-agnostic and focuses on system boundaries, contracts, data flow, storage, reliability, and verification.

## Design Summary

MixAPI is a unified LLM gateway with one stable public API and a control plane for model metadata, provider credentials, route policy, quotas, budgets, usage, and observability. The MVP should be built as a modular monolith with strong internal boundaries and a stateless data-plane hot path. This gives the first implementation low operational complexity while preserving the ability to split high-load pieces into separate services later.

The most important design rule is that MixAPI provides transport and operational consistency, not semantic equivalence across all models. The system must reject unsupported requests explicitly, route only to eligible providers, and expose provider-native options through a contained escape hatch.

## Goals

1. Implement `/v1/models`, `/v1/responses`, and `/v1/embeddings`.
2. Support OpenAI, Anthropic, Gemini, and one local backend such as Ollama.
3. Route by hard eligibility constraints before scoring by cost, latency, reliability, and tenant preferences.
4. Enforce API-key access, model allowlists, quotas, and budget gates.
5. Normalize provider responses, usage, cost, route metadata, and errors.
6. Preserve streaming behavior without corrupting or reordering stream frames.
7. Emit auditable usage events, route decision traces, request logs, metrics, and traces.
8. Keep provider-specific features available through `native.provider_options`.

## Non-Goals

1. Do not implement every provider-native feature in the portable request model.
2. Do not silently downgrade or drop unsupported request features.
3. Do not create a full billing, invoicing, or payment product in the MVP.
4. Do not make the plugin SDK or MCP connector system part of the P0 data path.
5. Do not support subscription account pooling patterns that may violate provider terms.

## Architecture Decision

### Recommended Approach: Modular Monolith With Split-Ready Boundaries

The MVP should ship as one deployable application backed by PostgreSQL, Redis, and an observability collector. Internally, it should be organized into bounded modules:

1. Public API edge.
2. Control plane API.
3. Capability registry.
4. Route planner.
5. Adapter runtime.
6. Quota and budget service.
7. Usage ledger.
8. Policy evaluator.
9. Observability pipeline.

This approach is the best fit because the repository starts from documents only, the MVP has substantial domain complexity, and early product learning will likely change internal APIs. A modular monolith avoids premature distributed systems work while preserving clean boundaries for future split-out services.

### Alternatives Considered

| Option | Benefit | Drawback | Decision |
|---|---|---|---|
| Thin OpenAI-compatible proxy | Fastest to implement | Weak differentiation and hides provider differences | Reject |
| Microservices from day one | Independent scaling and fault isolation | High operational cost before traffic proves need | Defer |
| Modular monolith with split-ready modules | Fast enough to build, clear boundaries, lower ops burden | Requires discipline to keep module boundaries clean | Use |

## High-Level Topology

```text
                         CONTROL PLANE
  +------------------------------------------------------------+
  | Admin API / Admin UI                                      |
  | Provider credentials, logical models, policies, quotas     |
  | Usage views, route traces, audit events                    |
  +-----------------------------+------------------------------+
                                |
                                | writes config, invalidates cache
                                v
  +------------------------------------------------------------+
  | PostgreSQL                                                 |
  | tenants, projects, keys, providers, models, policies,      |
  | quotas, usage ledger, route traces, audit log              |
  +-----------------------------+------------------------------+
                                |
                                | hot cache, counters, locks
                                v
  +------------------------------------------------------------+
  | Redis                                                      |
  | quota buckets, config snapshots, circuit state,            |
  | idempotency state, response cache metadata                 |
  +------------------------------------------------------------+

                           DATA PLANE
  +------------------------------------------------------------+
  | Public API Edge                                            |
  | auth, validation, normalization, idempotency, streaming     |
  +-----------------------------+------------------------------+
                                |
                                v
  +------------------------------------------------------------+
  | Route Planner                                              |
  | capability filter, policy gate, budget gate, scoring,      |
  | fallback plan, route decision trace                        |
  +-----------------------------+------------------------------+
                                |
                                v
  +------------------------------------------------------------+
  | Adapter Runtime                                            |
  | OpenAI adapter, Anthropic adapter, Gemini adapter,          |
  | Ollama/local adapter                                       |
  +---------------+---------------+---------------+------------+
                  |               |               |
                  v               v               v
               OpenAI        Anthropic        Gemini/Ollama
```

## Runtime Components

### Public API Edge

The public API edge handles all external application traffic. It must remain stateless except for request-local state. It owns authentication, request validation, idempotency lookup, request normalization, streaming response mediation, and mapping internal errors to public errors.

Responsibilities:

1. Authenticate API keys.
2. Resolve tenant, project, scopes, quotas, and model allowlists.
3. Parse and validate request bodies against the OpenAPI and JSON Schema contracts.
4. Normalize OpenAI-compatible input into the internal request representation.
5. Reject unsupported request features before provider dispatch.
6. Attach request ID, trace ID, tenant ID, project ID, and idempotency key.
7. Invoke the route planner.
8. Dispatch through the adapter runtime.
9. Stream normalized output events or return a final response.
10. Emit usage, route, log, metric, and trace events.

The edge must not embed provider-specific request construction directly. Provider-specific translation belongs to adapters.

### Control Plane API

The control plane manages configuration and operator workflows. It should be available through an admin API first; an admin UI can be added once the core API is stable.

Responsibilities:

1. Manage tenants, projects, and API keys.
2. Manage providers and provider credentials.
3. Manage provider model metadata and logical model mappings.
4. Manage quotas, budgets, and routing policies.
5. View usage, route traces, audit logs, and operational health.
6. Publish config version changes to Redis for data-plane cache invalidation.

Control-plane writes go to PostgreSQL. The data plane reads from Redis-backed snapshots with bounded staleness.

### Capability Registry

The capability registry is the source of truth for what each model can do. It is used by `/v1/models`, validation, route eligibility, policy decisions, and operator dashboards.

Required capability fields:

| Field | Purpose |
|---|---|
| `provider` | Provider identifier such as `openai`, `anthropic`, `gemini`, or `ollama` |
| `provider_model_id` | Raw upstream model ID |
| `logical_model_id` | Stable model exposed to clients |
| `status` | `active`, `disabled`, `deprecated`, or `expired` |
| `context_window_tokens` | Maximum total context size |
| `max_output_tokens` | Maximum output size when known |
| `input_modalities` | Supported inputs such as text, image, file, audio |
| `output_modalities` | Supported outputs such as text, image, embedding |
| `tool_modes` | None, function tools, provider tools, or MCP-backed tools |
| `schema_support` | None, JSON mode, best-effort schema, strict JSON Schema |
| `streaming_support` | Whether streaming is supported and which event shape applies |
| `embeddings_support` | Whether the model supports embeddings |
| `retention_class` | Provider handling category used by policy |
| `regions` | Allowed or available processing regions |
| `pricing_version_id` | Link to price rules for usage estimates |
| `native_features` | Provider-specific feature flags |
| `unsupported_parameters` | Parameters that must be rejected for this model |

The registry must prefer explicit false values over missing data. Unknown capability should be treated as unsupported for routing.

### Route Planner

The route planner selects the provider and model for a request. It produces a route plan before dispatch and a decision trace for debugging.

Routing stages:

1. Resolve requested logical model.
2. Load candidate provider models.
3. Apply hard eligibility filters.
4. Evaluate tenant policy and model allowlists.
5. Estimate request cost.
6. Reserve quota and budget.
7. Score eligible candidates.
8. Build retry and fallback plan.
9. Return selected candidate and trace context.

Hard filters must run before scoring:

1. Required input modality.
2. Required output modality.
3. Context length.
4. Tool support.
5. Schema strictness.
6. Streaming support.
7. Region and data residency.
8. Retention class.
9. Tenant allowlist.
10. Provider health and circuit state.

Default scoring formula:

```text
score =
  cost_weight * normalized_cost_score +
  latency_weight * normalized_latency_score +
  reliability_weight * normalized_reliability_score +
  cache_weight * cache_affinity_score +
  preference_weight * tenant_preference_score
```

The `balanced` objective should use all terms. `lowest-cost`, `lowest-latency`, and `highest-reliability` should adjust weights but must not bypass hard filters.

### Adapter Runtime

Adapters translate between MixAPI's internal representation and provider-native APIs.

Every adapter must implement these phases:

```text
validate_capabilities(request, model_capabilities)
translate_request(internal_request, route_context)
dispatch(provider_request, timeout_context)
translate_response(provider_response, route_context)
translate_stream(provider_stream, route_context)
reconcile_usage_cost(provider_response, route_context)
health_check()
```

Adapters must be isolated enough that provider-specific logic does not leak into the public API edge or route planner. Native provider options are allowed only inside `native.provider_options` and only after adapter validation.

MVP adapters:

| Adapter | Responsibilities |
|---|---|
| OpenAI | `/v1/responses`, embeddings, streaming, strict structured output where supported, usage reconciliation |
| Anthropic | Messages-style request translation, streaming translation, tool and schema capability gating, usage reconciliation |
| Gemini | Native or OpenAI-compatible translation, structured output gating, multimodal gating, usage reconciliation |
| Ollama/local | Local OpenAI-compatible or native translation, model health checks, local capability metadata |

Provider-specific unsupported features must return `capability_unsupported` before upstream dispatch.

### Quota And Budget Service

The quota and budget service enforces request, token, and spend limits before dispatch. It uses Redis for hot-path atomic counters and PostgreSQL for durable ledger records.

Quota types:

1. Requests per minute.
2. Requests per day.
3. Input tokens per minute.
4. Output tokens per minute.
5. Estimated spend per day.
6. Estimated spend per month.
7. Concurrent requests per key or tenant.

Budget flow:

1. Estimate maximum request cost from input size, requested output cap, model price, and tool options.
2. Reserve quota and estimated budget before dispatch.
3. Dispatch request.
4. Reconcile actual usage after response.
5. Release unused reservation or record overage adjustment.
6. On failure before upstream billable work, release reservation.
7. On unknown provider outcome, mark usage as `pending_reconciliation`.

Redis operations should be atomic. If Redis is unavailable, the default behavior should be fail-closed for paid production tenants unless an operator enables a degraded-mode policy.

### Usage Ledger

The usage ledger is an append-only record of request economics and routing. It should support provider invoice reconciliation and customer-facing usage exports.

Billable units must be generic:

| Unit | Examples |
|---|---|
| `input_tokens` | Prompt, messages, documents, cached input |
| `output_tokens` | Generated text |
| `cached_input_tokens` | Provider prompt-cache hits |
| `embedding_tokens` | Embedding inputs |
| `image_units` | Image generation or image analysis units |
| `tool_calls` | Provider-hosted tool invocations |
| `search_units` | Web search or retrieval tool usage |
| `compute_seconds` | Container or code execution time |
| `request_units` | Providers that bill per request |

Usage events should be immutable. Corrections should be represented as adjustment events, not updates to historical records.

### Policy Evaluator

The MVP can implement local policy checks for model allowlists, budgets, quotas, and key scopes. The design must leave a clear boundary for OPA-backed policies in P1.

Policy inputs:

1. Tenant and project.
2. API key scopes.
3. Requested logical model.
4. Candidate provider model.
5. Input and output modalities.
6. Tool usage.
7. Region and retention class.
8. Estimated cost.
9. Request metadata.

Policy outputs:

1. Allow.
2. Deny with public reason.
3. Require approval.
4. Redact or transform.
5. Restrict candidates.

P0 should implement allow and deny only. Approval, redaction, and transform are P1 or later.

### Observability Pipeline

Observability is part of the product, not only operations. Every request should produce enough metadata to explain route selection, fallback, provider errors, cost, and latency without exposing sensitive prompt content by default.

Signals:

1. Structured logs.
2. Metrics.
3. Traces.
4. Usage events.
5. Route decision traces.
6. Audit logs.

Required metrics:

| Metric | Labels |
|---|---|
| `mixapi_requests_total` | tenant, project, endpoint, model, provider, status |
| `mixapi_request_latency_ms` | endpoint, model, provider |
| `mixapi_provider_latency_ms` | provider, provider_model |
| `mixapi_provider_errors_total` | provider, error_class |
| `mixapi_fallbacks_total` | tenant, model, from_provider, to_provider, reason |
| `mixapi_quota_denials_total` | tenant, project, quota_type |
| `mixapi_budget_denials_total` | tenant, project |
| `mixapi_usage_billable_units_total` | tenant, provider, model, unit_type |

Trace spans:

1. `api.authenticate`.
2. `api.validate`.
3. `routing.plan`.
4. `quota.reserve`.
5. `adapter.translate_request`.
6. `provider.dispatch`.
7. `adapter.translate_response`.
8. `usage.reconcile`.
9. `quota.finalize`.

Prompt and completion bodies should not be included in logs or traces by default.

## Public API Design

### Authentication

Clients authenticate with bearer API keys:

```http
Authorization: Bearer mxapi_...
```

API keys should be stored hashed. Only a short prefix should be visible in the admin API. Each key maps to tenant, project, scopes, model allowlist, quota policy, budget policy, and optional expiry.

### `GET /v1/models`

Returns logical models available to the caller. The response must include capability metadata, not just names.

Example response shape:

```json
{
  "object": "list",
  "data": [
    {
      "id": "mixapi/balanced-chat",
      "object": "model",
      "status": "active",
      "context_window_tokens": 128000,
      "input_modalities": ["text", "image"],
      "output_modalities": ["text"],
      "tool_modes": ["function"],
      "schema_support": "strict_json_schema",
      "streaming": true,
      "providers": ["openai", "anthropic"],
      "pricing": {
        "unit": "tokens",
        "currency": "usd"
      }
    }
  ]
}
```

### `POST /v1/responses`

Accepts the portable request envelope and optional provider-native options. It supports streaming through Server-Sent Events when `stream` is true.

Required behavior:

1. Validate portable request shape.
2. Validate `native.provider_options` against selected adapter when provider is pinned.
3. Reject unsupported features before dispatch.
4. Return normalized response metadata.
5. Include stable public error envelopes.

### `POST /v1/embeddings`

Accepts OpenAI-compatible embedding input and routes only to models with `embeddings_support = true`.

Anthropic and other providers without native embeddings should not be eligible unless a configured embedding-capable backend exists under that provider.

### Error Envelope

```json
{
  "error": {
    "type": "capability_unsupported",
    "code": "schema_strictness_not_supported",
    "message": "The requested logical model does not support strict JSON Schema on any eligible provider.",
    "request_id": "req_123",
    "trace_id": "trace_123"
  }
}
```

Public error types:

| Type | HTTP status | Meaning |
|---|---:|---|
| `authentication_failed` | 401 | Missing or invalid API key |
| `permission_denied` | 403 | Key, tenant, or policy does not allow request |
| `validation_error` | 400 | Request shape is invalid |
| `capability_unsupported` | 400 | No eligible provider supports the requested features |
| `quota_exceeded` | 429 | Request exceeds request or token quota |
| `budget_exceeded` | 402 | Request exceeds spend budget |
| `provider_rate_limited` | 429 | Provider rate limit after fallback is exhausted |
| `provider_unavailable` | 503 | Provider unavailable after fallback is exhausted |
| `upstream_timeout` | 504 | Provider timed out after fallback is exhausted |
| `stream_interrupted` | 502 | Provider stream failed after response began |
| `internal_error` | 500 | Unexpected server failure |

## Internal Request Model

The public API should be normalized into an internal request object before routing.

```json
{
  "request_id": "req_123",
  "trace_id": "trace_123",
  "tenant_id": "ten_123",
  "project_id": "prj_123",
  "api_key_id": "key_123",
  "endpoint": "responses",
  "logical_model": "mixapi/balanced-chat",
  "input": [],
  "tools": [],
  "response_format": {},
  "routing": {
    "objective": "balanced",
    "max_cost_usd": "0.03",
    "fallback_policy": "same-family-then-cheaper"
  },
  "native": {
    "provider": "auto",
    "provider_options": {}
  },
  "stream": false,
  "idempotency_key": null,
  "created_at": "2026-06-09T00:00:00Z"
}
```

Use strings or fixed-precision decimal types for money. Do not use floating point for persisted costs.

## Data Model

PostgreSQL is the durable source of truth. Redis is used for hot counters, config snapshots, circuit breakers, idempotency state, and cache indexes.

### Core Tables

| Table | Key fields | Notes |
|---|---|---|
| `tenants` | `id`, `name`, `status`, `created_at` | Top-level isolation boundary |
| `projects` | `id`, `tenant_id`, `name`, `status` | Groups API keys and usage |
| `api_keys` | `id`, `tenant_id`, `project_id`, `key_hash`, `key_prefix`, `scopes`, `expires_at`, `status` | Store hashes only |
| `providers` | `id`, `name`, `status`, `adapter_type`, `base_url`, `health_state` | Provider config without secrets |
| `provider_credentials` | `id`, `provider_id`, `tenant_id`, `secret_ref`, `status` | Secret value lives in secret manager or encrypted column |
| `provider_models` | `id`, `provider_id`, `provider_model_id`, `status`, `metadata` | Raw upstream models |
| `logical_models` | `id`, `name`, `status`, `description`, `default_routing_policy_id` | Public model names |
| `logical_model_candidates` | `logical_model_id`, `provider_model_id`, `priority`, `weight`, `status` | Candidate pool |
| `capability_records` | `provider_model_id`, modality fields, schema fields, context limits, region, retention, `metadata` | Routing source |
| `pricing_versions` | `id`, `provider_model_id`, `effective_from`, `currency`, `rules` | Versioned price rules |
| `routing_policies` | `id`, `tenant_id`, `objective`, `fallback_policy`, `constraints` | Tenant or model policy |
| `quota_policies` | `id`, `tenant_id`, `project_id`, `limits` | Request, token, spend, concurrency |
| `usage_events` | `id`, `request_id`, `tenant_id`, `project_id`, `provider_id`, `model_id`, `units`, `costs`, `status` | Append-only |
| `route_decisions` | `id`, `request_id`, `tenant_id`, `selected_candidate`, `candidates`, `filters`, `scores`, `fallbacks` | Debug and audit |
| `audit_events` | `id`, `actor_id`, `tenant_id`, `action`, `target_type`, `target_id`, `diff`, `created_at` | Control-plane changes |
| `idempotency_keys` | `tenant_id`, `key`, `request_hash`, `response_ref`, `status`, `expires_at` | Duplicate request protection |

### Redis Keys

| Key pattern | Purpose |
|---|---|
| `cfg:{tenant_id}:version` | Current config version |
| `cfg:{tenant_id}:snapshot` | Cached tenant routing and quota config |
| `quota:{tenant_id}:{bucket}` | Token bucket counters |
| `budget:{tenant_id}:{period}` | Spend reservations and actualized spend |
| `circuit:{provider}:{model}` | Circuit breaker state |
| `idem:{tenant_id}:{key}` | Hot idempotency state |
| `health:{provider}` | Provider health summary |

## Request Flows

### Non-Streaming Response Flow

```text
client
  -> public api edge
  -> authenticate key
  -> validate request
  -> normalize request
  -> load tenant config snapshot
  -> route planner filters candidates
  -> policy evaluator allows or denies
  -> quota service reserves budget and counters
  -> adapter translates request
  -> provider dispatch
  -> adapter translates response
  -> usage ledger records billable units
  -> quota service finalizes reservation
  -> route decision is persisted
  -> normalized response returned to client
```

### Streaming Response Flow

```text
client
  -> public api edge
  -> authenticate, validate, normalize, route, reserve
  -> adapter opens provider stream
  -> edge sends response.created
  -> adapter translates provider chunks into normalized events
  -> edge flushes events in original order
  -> provider stream completes
  -> adapter reconciles final usage
  -> ledger and route decision are persisted
  -> edge sends response.completed
```

Fallback rules for streaming:

1. If failure occurs before the first client-visible event, fallback can proceed normally.
2. If failure occurs after output has begun, do not silently switch providers.
3. After stream start, emit `stream_interrupted` and record partial usage.
4. Client-side retry should use an idempotency key when the application wants duplicate protection.

### Fallback Flow

```text
attempt 1 selected
  -> provider timeout
  -> classify error
  -> check retry budget
  -> check provider billed status if known
  -> mark circuit health
  -> select next eligible fallback
  -> dispatch attempt 2
  -> persist both attempts in route decision
```

Duplicate-billing protection is best-effort because providers differ in idempotency support. MixAPI must prevent internal duplicate ledger records even when upstream billing status is unknown.

## Routing Details

### Candidate Eligibility

Each candidate is either eligible or rejected with a machine-readable reason. Store rejected candidates in the route decision trace.

Example rejection reasons:

1. `model_disabled`.
2. `tenant_not_allowed`.
3. `missing_input_modality`.
4. `missing_output_modality`.
5. `context_window_exceeded`.
6. `tools_not_supported`.
7. `strict_schema_not_supported`.
8. `region_not_allowed`.
9. `retention_class_not_allowed`.
10. `quota_unavailable`.
11. `provider_circuit_open`.

### Circuit Breakers

Maintain provider-model circuit state in Redis. Update state from provider errors, timeouts, and health checks.

States:

1. `closed`: normal routing.
2. `open`: do not route except explicit override.
3. `half_open`: allow limited probe traffic.

Circuit breaker decisions should affect eligibility, not only scoring.

### Fallback Policy

MVP fallback policies:

| Policy | Behavior |
|---|---|
| `none` | One attempt only |
| `same-provider` | Retry another model under the same provider if eligible |
| `same-family-then-cheaper` | Try comparable model first, then lower-cost eligible candidate |
| `lowest-latency` | Try next eligible candidate by latency score |
| `highest-reliability` | Try next eligible candidate by health and success rate |

Fallback should be disabled for non-idempotent tool calls unless the tool execution phase has not started.

## Provider Adapter Contracts

Adapters must treat the internal request as immutable. Any provider-specific mutation happens in the translated provider request.

Pseudo-interface:

```ts
interface ProviderAdapter {
  name: string;

  validateCapabilities(
    request: InternalRequest,
    capabilities: CapabilityRecord
  ): CapabilityValidationResult;

  translateRequest(
    request: InternalRequest,
    context: RouteContext
  ): ProviderRequest;

  dispatch(
    request: ProviderRequest,
    context: TimeoutContext
  ): Promise<ProviderResponse>;

  stream(
    request: ProviderRequest,
    context: TimeoutContext
  ): AsyncIterable<ProviderStreamEvent>;

  translateResponse(
    response: ProviderResponse,
    context: RouteContext
  ): InternalResponse;

  translateStreamEvent(
    event: ProviderStreamEvent,
    context: RouteContext
  ): InternalStreamEvent;

  reconcileUsageCost(
    response: ProviderResponse | ProviderStreamSummary,
    context: RouteContext
  ): UsageReconciliation;

  healthCheck(): Promise<ProviderHealth>;
}
```

Adapter conformance tests should run against a fake provider and, optionally, real provider sandbox credentials.

## Security Design

### API Keys

1. Generate high-entropy keys with a visible prefix.
2. Store only a salted hash and short display prefix.
3. Allow key scoping by tenant, project, model allowlist, endpoint, and budget.
4. Support expiry and revocation.
5. Never log full keys.

### Provider Secrets

Provider credentials should be stored in a secret manager when available. If the MVP uses encrypted database columns, encryption keys must be supplied externally and rotated through an operator process.

Secrets should be loaded only by the adapter runtime and never returned through the admin API.

### Data Handling

Default policy:

1. Do not persist prompt or completion bodies.
2. Persist metadata, usage, cost, route decisions, and error classes.
3. Allow optional content logging only with tenant-level explicit opt-in.
4. Redact authorization headers and provider keys from all logs.
5. Track retention class at model and request level.

### Admin Access

P0 can use admin API keys or local operator credentials. P1 should add OIDC, SSO, RBAC, and ABAC.

Admin actions must write audit events.

## Failure Handling

### Retry Matrix

| Error class | Retry same provider | Fallback provider | Notes |
|---|---|---|---|
| Provider 429 | No by default | Yes | Respect retry-after when available |
| Provider 5xx | Yes | Yes | Bounded by retry budget |
| Timeout before first byte | Yes | Yes | Safe if provider outcome unknown is tracked |
| Timeout after stream begins | No | No | Emit stream interruption |
| Invalid provider request | No | No | Adapter or capability bug |
| Capability unsupported | No | No | Must be caught before dispatch |
| Budget or quota denied | No | No | Client-visible denial |
| Policy denied | No | No | Client-visible denial |

### Idempotency

Support an optional `Idempotency-Key` header for mutating endpoints. The stored idempotency record should include tenant ID, request hash, response status, response metadata reference, and expiry.

If the same key is reused with a different request hash, return a validation error.

### Degraded Modes

Operator-configurable degraded modes:

1. Fail closed when Redis is unavailable.
2. Fail open for quota-only checks for selected internal tenants.
3. Disable fallback globally.
4. Disable a provider or model globally.
5. Force a logical model to one provider during incident response.

Production default should be fail closed for auth, policy, and budget checks.

## Deployment Design

### MVP Deployment

```text
Docker Compose or single Kubernetes namespace

app:
  MixAPI modular monolith

postgres:
  durable system of record

redis:
  quotas, counters, cache snapshots, circuits

otel-collector:
  traces and metrics export

optional local backend:
  ollama or compatible local inference server
```

The app should be horizontally scalable. Instances must not require local sticky state. Sticky routing to provider accounts or local backends, if needed later, should be modeled as route affinity metadata rather than load balancer affinity.

### Configuration

Runtime configuration should come from environment variables and database-backed control-plane settings.

Required environment categories:

1. Database URL.
2. Redis URL.
3. Encryption or secret manager configuration.
4. Public API base URL.
5. Provider default timeouts.
6. Observability exporter configuration.
7. Admin bootstrap credentials.

### Future Split Points

If traffic or team ownership requires it, split in this order:

1. Adapter workers for slow or high-volume provider calls.
2. Usage ledger writer for high-throughput event ingestion.
3. Control plane API and admin UI.
4. Route planner service.
5. Policy service with OPA sidecar or centralized OPA.

## Performance Design

The data-plane hot path should avoid database reads where possible.

Hot-path data sources:

1. API key metadata cache.
2. Tenant config snapshot.
3. Capability registry snapshot.
4. Redis quota counters.
5. Redis circuit state.

PostgreSQL writes for usage and route decisions can be synchronous for MVP correctness. If latency becomes an issue, write to an internal durable queue or outbox table and return after essential usage state is persisted.

Performance targets:

| Target | MVP goal |
|---|---|
| Added non-streaming median latency | Under 20 ms in same-region synthetic tests |
| Config snapshot lookup | Under 2 ms p95 from Redis |
| Quota reservation | Under 5 ms p95 from Redis |
| Route planning | Under 10 ms p95 for fewer than 100 candidates |
| Stream event translation | No buffering beyond one provider event unless required for normalization |

## Testing Strategy

### Unit Tests

1. Request normalization.
2. Capability validation.
3. Routing filters.
4. Scoring objectives.
5. Budget estimation.
6. Quota reservation and finalization.
7. Error mapping.
8. Usage reconciliation.

### Contract Tests

1. OpenAPI request and response schema validation.
2. Adapter interface conformance.
3. Provider fake-server tests.
4. Streaming event shape tests.
5. Error envelope stability.

### Integration Tests

1. `/v1/models` returns tenant-scoped model capabilities.
2. `/v1/responses` routes to the expected fake provider.
3. Unsupported features are rejected before upstream dispatch.
4. Quota and budget denials occur before provider calls.
5. Fallback occurs for injected 429, 5xx, and timeout failures.
6. Idempotency returns the original response for repeated keys.
7. Usage events and route decisions are written once per request.

### Load And Fault Tests

1. Synthetic same-region latency benchmark.
2. Concurrent quota reservation correctness.
3. Provider outage fallback.
4. Redis unavailable behavior.
5. PostgreSQL write latency and backpressure.
6. Stream interruption after partial output.
7. Circuit breaker open and half-open transitions.

### Security Tests

1. API keys are hashed and full keys are never logged.
2. Tenant-scoped keys cannot access other tenant resources.
3. Admin actions create audit events.
4. Provider credentials are not returned through APIs.
5. Prompt bodies are not persisted unless content logging is explicitly enabled.

## OpenAPI And SDK Generation

The public API should be spec-first. Maintain an OpenAPI document as the source of truth for public endpoints and generate SDKs from it once the schema stabilizes.

Spec requirements:

1. Model capability response schemas.
2. Responses request and response schemas.
3. Streaming event schemas.
4. Embeddings schemas.
5. Error envelope schemas.
6. Admin API schemas when exposed.

Do not generate SDKs from provider-native schemas. SDKs should target the MixAPI public contract.

## Migration And Rollout

### Phase 1: Technical Foundation

1. Define OpenAPI spec.
2. Create database migrations.
3. Implement API-key auth.
4. Implement capability registry.
5. Implement provider adapter interface and fake provider.
6. Implement `/v1/models`.

### Phase 2: First Routed Calls

1. Implement route planner.
2. Implement quota and budget reservation.
3. Implement OpenAI adapter.
4. Implement `/v1/responses`.
5. Persist usage and route decisions.
6. Add conformance tests.

### Phase 3: Multi-Provider MVP

1. Add Anthropic, Gemini, and Ollama adapters.
2. Add `/v1/embeddings`.
3. Add fallback policies.
4. Add streaming normalization.
5. Add circuit breakers.
6. Add usage export.

### Phase 4: Operator Readiness

1. Add admin API for provider and model management.
2. Add audit logs.
3. Add dashboards or metric exports.
4. Add structured output validation where supported.
5. Add deployment guide and runbooks.

## Acceptance Criteria

The technical MVP is ready when:

1. `/v1/models`, `/v1/responses`, and `/v1/embeddings` are implemented.
2. Four adapters exist: OpenAI, Anthropic, Gemini, and Ollama or another local backend.
3. Capability checks prevent unsupported dispatches in tests.
4. Quotas and budgets are enforced before provider calls.
5. Fallback succeeds for injected 429, 5xx, and pre-stream timeout scenarios.
6. Streaming tests show ordered, valid event output.
7. Usage events and route decision traces are persisted for every completed or failed routed request.
8. Public errors use the normalized error envelope.
9. API keys and provider credentials are not exposed in logs or API responses.
10. Synthetic gateway overhead meets the PRD target.

## Open Technical Questions

1. Which implementation language and web framework should be used?
2. Should provider calls run in-process for MVP, or through an adapter-worker queue from the start?
3. Should PostgreSQL writes for route decisions be synchronous on the request path or buffered through an outbox?
4. Which local backend should be P0: Ollama, Workers AI, or another target?
5. Should strict structured output validation be P0 for supported providers or P1 across adapters?
6. What tenant data retention defaults should apply to request metadata and route traces?
7. Should route decision traces be visible to developer API keys, admin users only, or controlled per tenant?

