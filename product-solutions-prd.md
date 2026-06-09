# Product Requirements: MixAPI Unified LLM Gateway

## Source

This PRD is derived from `deep-research-report.md`. It translates the research into a buildable product direction, MVP scope, requirements, success metrics, and rollout plan.

## Product Thesis

Teams adopting LLMs need a stable API surface, predictable operations, and provider flexibility, but they should not be forced into the false assumption that every model behaves the same. MixAPI should become a unified LLM gateway that guarantees transport stability, routing control, cost visibility, quota enforcement, and observability while preserving provider-native capabilities through explicit escape hatches.

The core principle is: portable by default, native when justified, explicit about differences.

## Target Users

| Persona | Problem | Desired outcome |
|---|---|---|
| AI application developer | Provider APIs differ across request shape, streaming, tools, schemas, and billing | One reliable API and SDK with clear capability metadata |
| Platform engineer | Teams are creating separate LLM integrations with inconsistent secrets, budgets, logs, and retries | Centralized gateway with policy, routing, and observability |
| Engineering manager | LLM spend and reliability are hard to manage across teams and providers | Budgets, usage reports, fallback behavior, and SLO visibility |
| Security or governance owner | Model and tool usage can violate data, region, retention, or approval rules | Enforceable policy controls and audit trails |

## Problem Statement

Current LLM integrations are fragmented. Teams must handle provider-specific APIs, model capability differences, fallback logic, streaming behavior, rate limits, usage accounting, schema validation, safety policy, and observability in each application. Existing gateway products solve parts of this problem, but many either act as thin OpenAI-compatible proxies or hide too much provider behavior.

MixAPI should solve the operational problem without pretending the semantic problem is fully abstractable.

## Product Goals

1. Provide a stable OpenAI-compatible core API for common LLM workflows.
2. Publish a capability-aware model catalog so clients know which features are supported.
3. Route requests by eligibility first, then by policy, budget, cost, latency, and reliability.
4. Normalize usage, cost, traces, and errors across providers.
5. Preserve provider-native features through an explicit `native` escape hatch.
6. Give operators controls for keys, tenants, quotas, budgets, and policy.
7. Keep the data-plane hot path small enough to add minimal latency.

## Non-Goals

1. Do not guarantee identical model behavior across providers.
2. Do not support every provider and every modality in the first release.
3. Do not build a full billing business system in the MVP.
4. Do not create a marketplace for plugins or connectors in the MVP.
5. Do not support high-risk subscription account sharing patterns as a general product feature.

## Solution Options

### Option 1: Thin API Relay

Build a simple OpenAI-compatible proxy with provider adapters, API keys, and basic failover.

Pros: fastest to ship, easiest to explain, low operational complexity.

Cons: weak differentiation, limited enterprise value, high risk of hiding provider differences, poor foundation for governance and observability.

### Option 2: Gateway Plus Control Plane

Build a stable data-plane gateway backed by a control plane for model catalog, provider credentials, routing policies, quotas, usage, and traces.

Pros: directly addresses the strongest market pain, supports enterprise adoption, creates a defensible product surface, and remains realistic about model differences.

Cons: more complex than a proxy, requires careful scoping to avoid building too much at once.

### Option 3: Extensible LLM Platform

Build the gateway plus plugin SDK, MCP connector registry, policy engine, marketplace, and partner provider metadata ingestion.

Pros: strongest long-term ecosystem play.

Cons: too broad for MVP; risks delaying validation of the core gateway.

### Recommendation

Use Option 2 for the MVP. It is the smallest credible product that matches the research conclusion: the market needs a control-plane plus data-plane gateway, not only an OpenAI-compatible relay. Option 1 can be an implementation stepping stone, and Option 3 should become the expansion roadmap after core reliability, routing, and usage accounting are proven.

## MVP Scope

### P0 Capabilities

| Area | Requirement |
|---|---|
| Public API | Implement `/v1/models`, `/v1/responses`, and `/v1/embeddings` |
| Provider adapters | Support OpenAI, Anthropic, Gemini, and one local provider such as Ollama |
| Request model | Accept OpenAI-style requests with normalized text, file, tool, and structured output fields where supported |
| Native escape hatch | Allow provider-specific options under a `native` object without polluting the portable core |
| Model catalog | Expose model metadata including provider, context length, modalities, pricing basis, supported parameters, tools, schema support, retention class, and deprecation status |
| Routing | Filter by capability, policy, budget, and tenant allowlist before scoring candidates |
| Fallbacks | Support provider fallback and model fallback with clear response metadata |
| Usage ledger | Record normalized usage, billable units, provider cost estimate, platform cost estimate, tenant, key, project, model, and provider |
| Quotas | Enforce request, token, and spend quotas at tenant and API-key level |
| Observability | Emit request logs, route decisions, latency, provider errors, fallback status, and usage events |
| Streaming | Preserve stream order and provider error semantics in normalized streaming responses |
| Auth | Support API keys for service access and admin credentials for operators |

### P1 Capabilities

| Area | Requirement |
|---|---|
| Admin UI | Manage providers, keys, model allowlists, quotas, budgets, and routing policies |
| Policy engine | Add OPA-backed policy decisions for model access, region, retention, and tool approvals |
| Structured outputs | Add provider-aware JSON Schema validation, retry, and schema-failure reporting |
| Caching | Add exact response cache and prompt-cache accounting where provider support exists |
| Tracing | Add OpenTelemetry-compatible traces with GenAI attributes |
| Shadow traffic | Allow a request to be mirrored to a secondary provider for evaluation without user-visible output |

### P2 Capabilities

| Area | Requirement |
|---|---|
| MCP connectors | Standardize tools and external context through MCP-compatible connectors |
| Advanced routing | Add weighted objectives for cost, latency, throughput, reliability, and cache affinity |
| Enterprise auth | Add OIDC, SSO, RBAC, and ABAC |
| Billing workflows | Add invoices, payment integration, credits, and customer-facing billing dashboards |
| Plugin SDK | Support request rewrite, redaction, approval, caching, and custom telemetry plugins |
| Regional routing | Enforce data residency and provider region constraints |

## Key User Stories

1. As an AI developer, I can call one `/v1/responses` endpoint and switch logical models without rewriting application code.
2. As an AI developer, I can inspect model capabilities before using tools, image input, strict schemas, or long context.
3. As a platform engineer, I can add provider keys once and expose approved logical models to application teams.
4. As a platform engineer, I can define fallback behavior so provider outages do not take down user workflows.
5. As a finance owner, I can see cost by tenant, project, key, model, provider, and time period.
6. As a security owner, I can block requests that violate retention, region, model, or tool policy.
7. As an operator, I can debug why a request used a specific provider and whether fallback occurred.

## Core Workflows

### Developer Integration

1. Developer creates or receives an API key.
2. Developer lists available models with `/v1/models`.
3. Developer selects a logical model such as `mixapi/balanced-chat`.
4. Developer sends a request to `/v1/responses`.
5. MixAPI validates the request, routes it to an eligible provider, streams or returns output, and includes usage and route metadata.

### Operator Onboarding A Provider

1. Operator adds provider credentials.
2. MixAPI fetches or imports provider model metadata.
3. Operator maps provider models to logical models.
4. Operator sets pricing, retention class, region, capabilities, and allowed tenants.
5. Operator enables the provider in routing policies.

### Routed Request

1. Normalize and validate request.
2. Resolve logical model to candidate providers.
3. Filter by modality, tools, schema support, context length, policy, budget, region, and tenant allowlist.
4. Score remaining candidates by objective.
5. Dispatch to top candidate.
6. Retry or fall back within configured limits.
7. Translate response, reconcile usage and cost, emit logs and traces.

## Functional Requirements

### Public API

| ID | Requirement | Priority |
|---|---|---|
| API-1 | Provide `/v1/models` with capability-rich model metadata | P0 |
| API-2 | Provide `/v1/responses` for text generation, multimodal input metadata, tools, structured output hints, and streaming | P0 |
| API-3 | Provide `/v1/embeddings` with normalized usage accounting | P0 |
| API-4 | Return stable normalized errors with provider error detail available to authorized operators | P0 |
| API-5 | Include route metadata in responses when enabled by tenant policy | P0 |
| API-6 | Support provider-native options under `native.provider_options` | P0 |

### Routing

| ID | Requirement | Priority |
|---|---|---|
| RTE-1 | Apply hard eligibility filters before cost or latency scoring | P0 |
| RTE-2 | Support objectives: balanced, lowest-cost, lowest-latency, highest-reliability | P0 |
| RTE-3 | Enforce max cost, quota, and tenant allowlist before dispatch | P0 |
| RTE-4 | Support retry, provider fallback, and model fallback with duplicate-billing protection | P0 |
| RTE-5 | Record decision trace for debugging | P0 |

### Capability Registry

| ID | Requirement | Priority |
|---|---|---|
| CAP-1 | Store provider, model ID, logical model mapping, context length, modality, tool support, schema strictness, pricing, region, and retention class | P0 |
| CAP-2 | Mark unsupported features explicitly instead of silently dropping them | P0 |
| CAP-3 | Support deprecation and expiration metadata | P1 |

### Usage And Cost

| ID | Requirement | Priority |
|---|---|---|
| CST-1 | Record normalized input tokens, output tokens, cached tokens, request count, and provider-specific billable units | P0 |
| CST-2 | Estimate provider cost and platform cost per request | P0 |
| CST-3 | Enforce tenant and key-level budgets | P0 |
| CST-4 | Export usage as CSV or API responses for finance workflows | P1 |

### Governance

| ID | Requirement | Priority |
|---|---|---|
| GOV-1 | Enforce API-key authentication on all public endpoints | P0 |
| GOV-2 | Scope keys to tenant, project, model allowlist, and budget | P0 |
| GOV-3 | Support policy checks for model, tool, region, and retention constraints | P1 |
| GOV-4 | Log admin changes to providers, keys, policies, and budgets | P1 |

### Observability

| ID | Requirement | Priority |
|---|---|---|
| OBS-1 | Record latency, provider, model, route attempt count, fallback status, error class, and usage | P0 |
| OBS-2 | Provide request-level trace IDs in responses | P0 |
| OBS-3 | Emit OpenTelemetry-compatible spans | P1 |
| OBS-4 | Provide dashboards for spend, errors, latency, fallback rate, and top models | P1 |

## Proposed API Shape

### Request

```json
{
  "model": "mixapi/balanced-chat",
  "input": [
    {
      "role": "user",
      "content": [
        { "type": "input_text", "text": "Summarize this document." }
      ]
    }
  ],
  "response": {
    "format": {
      "type": "json_schema",
      "json_schema": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object"
      }
    }
  },
  "routing": {
    "objective": "balanced",
    "max_cost_usd": 0.03,
    "fallback_policy": "same-family-then-cheaper"
  },
  "native": {
    "provider": "auto",
    "provider_options": {}
  }
}
```

### Response Metadata

```json
{
  "id": "resp_123",
  "model": "mixapi/balanced-chat",
  "provider": "openai",
  "usage": {
    "input_tokens": 1200,
    "output_tokens": 350,
    "cached_input_tokens": 0,
    "billable_units": []
  },
  "cost": {
    "provider_cost_usd": 0.0124,
    "platform_cost_usd": 0.013
  },
  "route": {
    "attempts": 1,
    "latency_ms": 842,
    "fallback_used": false,
    "decision_trace_id": "trace_123"
  }
}
```

## Data Model

| Entity | Purpose |
|---|---|
| Tenant | Billing, policy, and access boundary |
| Project | Application-level grouping under a tenant |
| API key | Service credential with scopes, quotas, and budgets |
| Provider | OpenAI, Anthropic, Gemini, Ollama, or other backend |
| Provider credential | Secret material for provider access |
| Provider model | Raw model exposed by a provider |
| Logical model | Stable model name exposed to clients |
| Capability record | Feature metadata used for validation and routing |
| Routing policy | Objective, fallback, constraints, and allowlists |
| Usage event | Request-level usage and cost record |
| Trace event | Request-level route and observability record |
| Policy event | Allow, deny, redact, approve, or fallback decision |

## Success Metrics

| Metric | MVP target |
|---|---|
| Unsupported dispatches | 0 known unsupported-model dispatches in test suite |
| Gateway overhead | Median added latency under 20 ms in same-region synthetic tests |
| Streaming integrity | No out-of-order chunks or corrupted stream frames in conformance tests |
| Usage accuracy | Less than 1% discrepancy between estimated and provider-reported billable units for supported providers |
| Fallback reliability | Successful fallback within configured retry budget for injected 429, 5xx, and timeout scenarios |
| Policy correctness | 100% pass rate for model allowlist, budget, and quota enforcement tests |
| Developer activation | First successful `/v1/responses` call within 10 minutes of key creation |

## Rollout Plan

### Phase 1: Internal Alpha

Build the data-plane gateway, provider adapters, model catalog, API-key auth, quotas, and usage ledger. Validate with synthetic workloads and a small set of internal applications.

Exit criteria: `/v1/models`, `/v1/responses`, `/v1/embeddings`, four adapters, streaming, fallback, quota enforcement, and usage logs pass conformance tests.

### Phase 2: Private Beta

Add admin UI, dashboards, structured output validation, richer routing objectives, and exportable usage. Onboard design partners with multi-provider needs.

Exit criteria: at least three external tenants route production-like traffic with no critical billing, auth, or unsupported-dispatch defects.

### Phase 3: GA

Add OIDC, OpenTelemetry, policy engine integration, enterprise audit logs, documented SLAs, and production deployment guides.

Exit criteria: documented operational runbooks, provider failure playbooks, security review, compatibility guides, and published API spec.

## Risks And Mitigations

| Risk | Mitigation |
|---|---|
| Provider APIs change quickly | Treat adapters as versioned modules with conformance tests and capability flags |
| Abstraction hides important model differences | Expose capabilities and native options explicitly |
| Cost estimates drift from provider invoices | Reconcile provider-reported usage and retain billable unit details |
| Routing creates confusing behavior | Include route metadata and decision traces |
| Policy engine slows the hot path | Cache policy decisions where safe and keep hard eligibility checks local |
| Streaming behavior differs by provider | Use provider-specific stream translators and stream conformance tests |
| MVP becomes too broad | Keep P0 limited to three endpoints, four adapters, basic routing, quotas, usage, and logs |

## Open Questions

1. Should the first customer path prioritize self-hosted deployment, managed cloud, or both?
2. Which local or edge backend should be the P0 non-cloud adapter: Ollama, Workers AI, or another target?
3. Should structured output validation be P0 for one provider or P1 across all providers?
4. Should route decision metadata be visible to developers by default or restricted to operators?
5. What is the initial pricing model: gateway usage markup, subscription, seat-based control plane, or open-source core plus managed services?

