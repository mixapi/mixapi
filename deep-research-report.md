# Unified Interface for Large Language Models

A unified LLM interface should not be designed as a thin “OpenAI-compatible proxy” alone. The strongest pattern emerging across the market is a **control plane plus data plane** gateway that offers one stable developer surface while still preserving access to provider-native capabilities, routing policies, budgets, safety controls, and rich telemetry. That pattern is visible in managed gateways such as OpenRouter, Vercel AI Gateway, and Cloudflare AI Gateway, and in open-source/self-hosted systems such as New API, One API, sub2api, and LiteLLM. 

The single most important design conclusion is this: **a unified API should guarantee transport stability and operational consistency, not pretend that all models are semantically identical**. Provider APIs now differ on statefulness, tool models, schema strictness, versioning, billing units, and multimodal capabilities. OpenAI’s Responses API supports text and image inputs, built-in tools, and stateful interactions; Anthropic’s API centers on Messages and requires an `anthropic-version` header; Gemini distinguishes stable `v1` from `v1beta` and also exposes an OpenAI-compatibility layer; Mistral spans chat, function calling, document AI/OCR, connectors, and self-deployment. 

## Landscape and governing principles

A strong unified interface should solve five recurring pains at once: provider switching, resilience and failover, cross-provider cost visibility, quota enforcement, and observability. Managed gateways explicitly market these benefits. Vercel AI Gateway documents a single endpoint with budgets, usage monitoring, load balancing, and fallbacks across hundreds of models; Cloudflare AI Gateway emphasizes analytics, logging, caching, rate limiting, retries, and model fallback; OpenRouter emphasizes unified routing, fallbacks, credits, and higher availability through pooled providers. 

At the same time, abstraction breaks down if the interface hides too much. Anthropic’s OpenAI SDK compatibility layer is explicitly framed as useful for evaluation and comparison, not as the best long-term production path for every Claude-native feature. New API likewise documents that some conversions remain partial, such as Gemini-to-OpenAI compatibility being text-only with function calling not yet supported, and OpenAI-compatible-to-Responses conversion still under development. Those examples show why a serious gateway must expose both a **portable core** and a **native escape hatch**. 

The best governing principle is therefore: **portable by default, native when justified, explicit about differences**. That principle is already implicit in the ecosystem. Gemini supports both its native API and OpenAI compatibility; Ollama supports both OpenAI and Anthropic compatibility for local or hybrid use; OpenRouter keeps a unified interface but also exposes router metadata and provider controls; New API and LiteLLM support multiple API formats and provider-specific routing or policy knobs. 

## Architecture patterns and recommended blueprint

Three architectural paradigms dominate this space. The first is the **centralized gateway**, which is simple to deploy and ideal for internal teams or smaller SaaS footprints; One API and New API are strong examples of this model, both documenting single-binary or Docker-oriented deployment and centralized relay behavior. The second is the **microgateway plus adapters** pattern, where the public ingress is stable but downstream provider adapters can scale, fail, and evolve independently. The third is a **plugin or extension-driver system**, analogous to Kong plugins and Envoy HTTP filters, with policy delegated to a service such as OPA. 

The most robust design for a modern unified LLM platform is a **hybrid**: keep a stable centralized API edge for developers, but separate control-plane configuration from data-plane execution, and run provider integrations as isolated adapters behind an internal routing layer. This matches how modern API gateways and AI gateways separate fast-path request handling from policy, metadata, and management functions. It also matches the scaling guidance in New API cluster deployment and One API multi-machine deployment, both of which depend on shared databases, Redis, and load balancing rather than a monolithic single host. 

The recommended blueprint below follows that pattern. It is grounded in gateway plugin/filter designs, policy-as-code engines, and the practical features exposed by current AI gateways. 

```text
                           CONTROL PLANE
┌─────────────────────────────────────────────────────────────────────┐
│  Admin UI / API                                                    │
│  Model Catalog + Capability Registry                               │
│  Secrets + Provider Keys / BYOK                                    │
│  Policy Sets + Tenant Config + Route Policies                      │
│  Usage Ledger + Billing Rules + Quotas                             │
│  Plugin Registry + SDK + Conformance Tests                         │
│  Analytics + SLOs + Audit Store                                    │
└─────────────────────────────────────────────────────────────────────┘

                               │ config / policy / metadata
                               ▼

                             DATA PLANE
┌─────────────────────────────────────────────────────────────────────┐
│  Public API Edge                                                   │
│  - AuthN/AuthZ                                                     │
│  - Idempotency + Rate Limits                                       │
│  - Request normalization                                           │
│  - Streaming broker                                                │
│  - Trace/span creation                                             │
│                                                                    │
│  Route Planner                                                     │
│  - Capability filter                                               │
│  - Policy checks                                                   │
│  - Budget gate                                                     │
│  - Cost/latency/reliability scoring                                │
│  - Fallback/circuit breaker                                        │
│                                                                    │
│  Adapter Runtime                                                   │
│  - OpenAI adapter                                                  │
│  - Anthropic adapter                                               │
│  - Gemini adapter                                                  │
│  - Mistral adapter                                                 │
│  - Local/edge adapters                                             │
│  - Plugin hooks (cache, rewrite, redact, approve, shadow)          │
└─────────────────────────────────────────────────────────────────────┘
            │                 │                 │                │
            ▼                 ▼                 ▼                ▼
        OpenAI            Anthropic           Google         Local/Edge
                                                Gemini       Ollama / Workers AI
```

This blueprint scales well because the data plane stays mostly stateless, while session state, quotas, consumption data, cache maps, and tenant configuration live in shared infrastructure. That is the same operational direction visible in New API cluster deployment, One API’s Redis-backed multi-machine mode, and sub2api’s dependency on PostgreSQL and Redis for production hosting. 

## Interface contract and adapter framework

The public contract should be **spec-first**. OpenAPI remains the standard ecosystem description format for HTTP APIs, and the JSON Schema 2020-12 family is the right basis for request validation, provider capability metadata, and structured output definitions. OpenAI’s official Python library is generated from OpenAPI, which is a useful precedent for generating reliable SDKs from a single source of truth. 

For maximum adoption, the public API should center on an OpenAI-compatible core because that is already the ecosystem’s de facto migration path. OpenRouter exposes an OpenAI-compatible Responses API; Vercel AI Gateway supports OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages; Gemini offers OpenAI compatibility; Anthropic offers OpenAI SDK compatibility for testing; LiteLLM and Ollama both standardize on OpenAI-shaped access for many providers or local models. A good public surface is therefore: `/v1/models`, `/v1/responses`, `/v1/embeddings`, `/v1/rerank`, `/v1/audio/transcriptions`, `/v1/images/generations`, `/v1/moderations`, `/v1/batches`, plus management endpoints for usage, billing, keys, and policies. 

The contract must also be **capability-aware** rather than pretending every endpoint feature exists everywhere. Structured outputs are now available across major providers, but the exact schemas and guarantees differ: OpenAI uses strict JSON Schema adherence, Anthropic exposes JSON outputs and strict tool use, Gemini supports structured outputs with tools and notes that unsupported schema fields may be ignored on some surfaces, and Mistral distinguishes JSON mode from stronger custom structured output. That means the gateway should publish a capability matrix per model and per adapter, not just a list of model names. 

OpenRouter’s model metadata is a strong precedent: its documented schema includes model identifiers, context length, architecture, input and output modalities, pricing, supported parameters, defaults, and expiration dates. A unified interface should adopt the same idea internally and extend it with fields like `statefulness`, `tool_modes`, `schema_strictness`, `data_retention_class`, `regionality`, `native_features`, and `deprecation_policy`. 

A practical request envelope for the unified API should look like this as a proposal:

```json
{
  "model": "logical-model-id",
  "input": [
    {
      "role": "user",
      "content": [
        { "type": "input_text", "text": "Summarize this PDF" },
        { "type": "input_file", "file_ref": "file_123" }
      ]
    }
  ],
  "tools": [
    {
      "type": "function",
      "name": "lookup_invoice",
      "description": "Fetch invoice details",
      "parameters": { "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object" }
    }
  ],
  "response": {
    "format": {
      "type": "json_schema",
      "json_schema": { "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object" }
    }
  },
  "routing": {
    "objective": "balanced",
    "max_cost_usd": 0.03,
    "preferred_region": "us",
    "fallback_policy": "same-family-then-cheaper"
  },
  "safety": {
    "policy_set": "enterprise-default",
    "approval_mode": "required-for-external-tools"
  },
  "native": {
    "provider": "auto",
    "provider_options": {}
  }
}
```

The companion response should normalize the payload but also carry explicit route and provenance metadata, inspired by OpenRouter’s router metadata and broader gateway observability patterns. 

```json
{
  "id": "resp_...",
  "model": "logical-model-id",
  "provider": "selected-provider",
  "output": [...],
  "usage": {
    "input_tokens": 1200,
    "output_tokens": 350,
    "cached_input_tokens": 800,
    "billable_units": [...]
  },
  "cost": {
    "provider_cost_usd": 0.0124,
    "platform_cost_usd": 0.0130
  },
  "route": {
    "attempts": 1,
    "latency_ms": 842,
    "fallback_used": false,
    "decision_trace": [...]
  },
  "safety": {
    "policy_actions": [...],
    "tool_approvals": [...]
  },
  "native": {
    "provider_response": {}
  }
}
```

The adapter layer should implement four mandatory phases: `translate_request`, `dispatch`, `translate_response`, and `reconcile_usage_cost`. Optional phases should support tool mediation, content rewriting, stream patching, shadow traffic, and safety review. Plugin hooks should behave like a gateway filter chain—very close to the plugin/filter models used in Kong and Envoy—and tools or connectors should be attached through an open extension contract rather than hardcoded into each adapter. MCP is the strongest emerging standard for those extensions because it provides a protocol, schemas, JSON-RPC message structure, and broad ecosystem support, while OpenAI and Mistral are already documenting connector and MCP-related integrations. 

## Routing, reliability, and cost intelligence

A good routing engine should not begin with “cheapest model wins.” It should begin with **eligibility**. The first stage should filter candidates by hard constraints: input modality, output modality, required tools, strict JSON Schema support, region or residency requirements, retention policy, and tenant-specific allowlists. OpenRouter’s routing docs make this explicit in practice: it routes only to providers that support required tools or requested token behavior, and its default strategy is price-oriented only after availability and capability constraints are satisfied. 

After eligibility, routing should use a weighted score spanning cost, p95 latency, current error rate, historical success rate, prompt-cache affinity, and budget posture. That is consistent with the strategies already documented by current platforms: OpenRouter supports price-, throughput-, and latency-oriented sorting; LiteLLM documents latency-based, least-busy, usage-based, and cost-based routing plus provider budget routing; Vercel and Cloudflare both expose fallbacks and usage controls. 

The routing hot path should look like this. The sequence combines ideas already visible in production gateways and adapter-based routers. 

```text
Request
  ↓
Normalize + validate
  ↓
Capability / policy filter
  ↓
Budget and quota gate
  ↓
Score candidates
  ├─ cost
  ├─ latency
  ├─ reliability
  ├─ cache affinity
  └─ tenant preferences
  ↓
Dispatch to best candidate
  ↓
Retry / provider fallback / model fallback
  ↓
Normalize response + reconcile usage/cost
  ↓
Emit traces, logs, and decision metadata
```

Not every backend should be treated as stateless. Sub2API is a useful example of a specialized relay where account selection, sticky sessions, and per-user or per-account concurrency matter, because upstream quotas are tied to subscription-backed accounts rather than fungible API capacity. That is a real design lesson: the platform should support **affinity routing** for providers, sessions, or accounts when the economics or semantics require it. 

Billing reconciliation is one of the hardest problems and is frequently underestimated. OpenAI’s pricing includes token rates plus separate tool pricing for features such as web search, file search, and containers; Anthropic’s pricing varies across direct and cloud-platform billing paths and notes tokenizer changes; Gemini uses free, prepaid, and pay-as-you-go tiers; Mistral’s billing is consolidated at the organization level with workspace spending caps. A unified platform therefore needs an internal ledger that supports multiple billable unit types instead of assuming everything is “input tokens plus output tokens.” 

Caching needs equally deliberate treatment. Cloudflare AI Gateway exposes caching as a first-class feature for speed and cost savings, and New API documents cache-hit billing controls. A mature unified gateway should therefore maintain at least three distinct layers: exact deterministic-response cache, prompt-cache accounting layer, and semantic retrieval cache for tool-driven workflows. 

## Security, governance, and observability

The platform should adopt a **zero-trust, multi-tenant** security model from the start. Identity should be based on OAuth 2.0 and OpenID Connect for human and service identities, with JWTs or mTLS for machine-to-machine calls. Authorization should combine RBAC and ABAC, because model choice, tool scopes, workspace, geography, data sensitivity, and billing account all affect whether a request should be allowed. OWASP’s API Security Top 10 remains directly relevant here, especially broken object-level authorization and related authorization failures. 

A separate policy engine is worth the complexity. OPA exists precisely to externalize policy decisions from application code, and that pattern maps extremely well to unified LLM gateways. Use it for model allowlists, tool-approval logic, data-retention rules, regional routing restrictions, prompt redaction, and tenant-specific constraints. For an enterprise governance baseline, align controls to the NIST AI RMF and, where assurance matters, to the AICPA trust services criteria used in SOC reporting. 

GenAI-specific risks deserve their own layer, not just generic API controls. OWASP’s LLM Top 10 highlights prompt injection, insecure output handling, training-data issues, and other risks that a unified gateway can help mitigate centrally. For tool use and remote connectors, require approval gates where the risk is high. OpenAI’s connectors and remote MCP guidance explicitly supports approval-based execution modes, which is a strong precedent for enterprise tool governance. 

Observability should be designed as a first-class product capability. OpenTelemetry now includes generative AI semantic conventions, and OpenInference extends OpenTelemetry with LLM- and tool-specific tracing semantics. Combined with gateway-specific route metadata—similar to OpenRouter’s router metadata—you can produce debuggable traces that explain why a provider was chosen, whether context was compressed, whether policy intervened, whether fallback occurred, and what cost was incurred. 

Data governance must be modeled at the request level. Some providers expose special handling modes that materially affect enterprise design; for example, Anthropic documents Claude PDF support as eligible for zero data retention in organizations with ZDR arrangements. Your gateway should therefore tag every model and feature with retention, residency, and provider-handling metadata so policy can route or block accordingly. 

## Comparative analysis of existing platforms

The current landscape divides into hosted gateways, self-hosted administrative relays, and specialized quota-redistribution systems. The table below highlights what each reference platform teaches a new design.

| Platform | Best fit | Strong documented capabilities | Main lesson for a new design | Evidence |
|---|---|---|---|---|
| **OpenRouter** | Managed multi-provider routing | OpenAI-compatible Responses API, model and provider fallback, price/throughput/latency-aware routing, credits, router metadata, workspace BYOK and guardrails | Best reference for hosted routing intelligence, route explainability, and provider-aware failover |  |
| **New API** | Self-hosted team or enterprise gateway | OpenAI Responses, Realtime, Claude Messages, Gemini, OIDC auth, billing, cache billing, retries, user-level model limits, cluster deployment with shared MySQL/Redis | Best reference for multi-protocol mediation and private deployment, but also a reminder that format conversion is often incomplete and should be capability-gated |  |
| **One API** | Simpler self-hosted relay and redistribution | Broad provider list, OpenAI-format unification, load balancing, token and channel management, multi-machine deployment with MySQL and Redis | Best reference for operational simplicity and OSS adoption, but a modern platform should go beyond relay and support richer native protocol features |  |
| **sub2api** | Subscription-quota redistribution and account sharing | Multi-account management, token-level billing, sticky sessions, concurrency control, rate limiting, built-in payments, admin dashboard | Best reference for affinity routing and quota-backed account pools, but it also highlights legal and ToS risk that should not be normalized into a general-purpose enterprise design |  |
| **LiteLLM** | Broad adapter breadth and extensible gateway | 100+ providers through OpenAI format, budgets, rate limits, multiple routing strategies, traffic mirroring, plugins, published benchmarks | Best reference for adapter extensibility and operational transparency; also an example that some routing strategies trade off performance and should be used selectively |  |
| **Vercel and Cloudflare AI gateways** | Managed control-plane patterns | One endpoint, budgets, usage monitoring, load balancing, fallbacks, analytics, logging, caching, rate limiting | Best references for commercial control-plane UX and operational productization around the gateway core |  |

Two gaps stand out across the market. First, **semantic portability remains incomplete**: OpenRouter’s Responses layer is stateless only, Anthropic says its OpenAI compatibility should not be treated as the long-term answer for every production use case, and New API explicitly documents partially supported conversions. Second, **plugin and connector standardization is still immature**, but MCP is clearly emerging as the most credible common denominator because it supplies protocol, schema, and broad client/server ecosystem support. 

## Scaling, ecosystem, and benchmark strategy

The strongest commercialization strategy is likely **open-source core plus managed control plane**. The open-source core should include the public API surface, adapter SDK, plugin SDK, model capability registry, routing engine, conformance suite, and baseline observability. The commercial layer should add hosted control-plane features that operators repeatedly pay for: BYOK, policy management, billing reconciliation, higher-SLA routing, analytics, enterprise auth, provider partnerships, and support. That split mirrors what the market is already rewarding in managed gateways while preserving the contributor energy seen in large open-source projects such as One API, New API, sub2api, and LiteLLM. 

Documentation and SDK tooling should be treated as a product moat, not as afterthoughts. Publish the API contract in OpenAPI, generate typed SDKs, maintain compatibility guides for OpenAI, Anthropic, Gemini, and Ollama flows, and expose a live capabilities matrix per model. This is exactly the kind of discipline that makes ecosystem adoption compound over time. 

For ecosystem growth, make extensions easy and opinionated. Use MCP for tools and context connectors; publish a plugin lifecycle for request rewriting, caching, redaction, approval, and shadow traffic; and create a partner model where providers can publish pricing, capability, and reliability metadata into your registry. OpenRouter’s provider integration requirements and the MCP ecosystems around Anthropic, OpenAI, and Mistral all point in this direction. 

Performance goals should be explicit. Lightweight gateways can be very fast under controlled conditions: LiteLLM publishes synthetic benchmarks reporting 8 ms p95 latency at 1k RPS against a fake OpenAI endpoint and a separate benchmark citing 3.25 ms added latency relative to raw OpenAI API. Those numbers should not be treated as universal production truth, but they do indicate that well-built gateway overhead can remain low if the hot path is small and heavy logic is pushed to metadata caches, Redis, and background services. 

A practical benchmark and PoC plan should cover the workloads below.

| Use case | Benchmark scenario | Primary metrics | Proposed success target |
|---|---|---|---|
| Interactive chat copilot | 1–4 KB prompts, streaming on | Added gateway latency, TTFT, stream continuity | Median added latency under 10 ms in same-region synthetic tests; no stream corruption |
| Tool-calling agent | 2–5 tool calls with approval gates | Tool schema validity, approval latency, fallback rate | 99% valid tool payloads; approval overhead under 250 ms excluding human wait |
| Structured extraction | Strict JSON Schema across providers | Schema pass rate, retry rate | 99% schema-valid output on supported-capability paths |
| RAG / search workflow | Embeddings + rerank + response | End-to-end latency, cache hit rate, answer consistency | 20%+ improvement from cache-enabled repeated workloads |
| Vision / document AI | PDF/image input and extraction | Capability routing accuracy, OCR or parse success | 100% capability-correct routing; no unsupported-model dispatches |
| Multi-tenant SaaS | 100–1,000 tenants, quotas enabled | Rate-limit correctness, noisy-neighbor isolation | No cross-tenant counter leakage; deterministic quota enforcement |
| Failure handling | provider 5xx / 429 / timeout injection | Recovery time, fallback success, duplicate billing defects | Successful failover within one retry budget; zero double-billing |

A good PoC can be intentionally small but should still prove the hard parts. The minimum credible prototype is: OpenAI, Anthropic, Gemini, and one local or edge backend such as Ollama or Workers AI; `/v1/models`, `/v1/responses`, and `/v1/embeddings`; JSON Schema structured output; OIDC or API-key auth; Redis-backed quotas; OPA-backed route policy; OpenTelemetry or OpenInference traces; and budget-aware fallback routing. Local and hybrid support matter because the market is moving there too: Mistral documents self-deployment, Ollama documents OpenAI and Anthropic compatibility for local APIs, and Cloudflare Workers AI pushes inference to the edge. 

The clearest product opportunity is to build a unified interface that combines the best parts of today’s offerings without inheriting their narrow assumptions: OpenRouter’s routing and metadata, New API’s protocol mediation and private deployment, One API’s simplicity, LiteLLM’s adapter breadth and published operational learning, MCP’s extension model, and enterprise-grade governance built on OIDC, OPA, OpenTelemetry, and NIST-style controls. That combination would produce a gateway that is not merely compatible, but strategically durable.
