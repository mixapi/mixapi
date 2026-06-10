# Observability Design

## Architecture

MixAPI will add a dependency-free observability boundary that records metrics and trace spans without choosing a deployment-specific backend. A new `mixapi.observability` module will define immutable metric and span records plus a thread-safe in-memory collector. `create_app` will accept an optional collector and expose the active collector through `app.state.observability`, matching the existing service-injection pattern for budgets, circuits, usage, and route decisions.

The collector will expose counter, histogram, gauge, and span APIs. Metric labels will be normalized into sorted tuples so snapshots are deterministic and safe to assert in tests. Spans will carry the existing request trace ID, a name, status, duration, and low-cardinality attributes. Request or response bodies, API keys, and prompt text will never be captured. In-memory event retention is bounded, and a safety wrapper prevents exporter failures from changing API behavior. This keeps the records compatible with later OpenTelemetry or Prometheus adapters while avoiding a runtime dependency and exporter configuration in the gateway core.

## Data Flow

Provider dispatch helpers will measure every attempt, including failures and stream initialization. Each attempt emits `mixapi_provider_latency_ms`, a `provider.dispatch` span, and on failure `mixapi_provider_errors_total`. When a later candidate succeeds, the endpoint emits one `mixapi_fallbacks_total` counter with tenant, model, source provider, destination provider, and failure reason labels.

Circuit filtering and state transitions emit `mixapi_circuit_state` gauges, where `1` means open and `0` means closed. Budget failures are recorded at both rejection points: request-level route cost filtering and API-key cumulative reservation. They emit `mixapi_budget_denials_total` and a failed `budget.reserve` span. Successful reservations emit a successful span but no denial metric.

The endpoint passes request trace context and tenant-safe routing attributes into helpers. Streaming records dispatch latency when the provider stream is opened and primed; post-first-event failures update provider errors and circuit state during stream finalization. Instrumentation failures must never alter request behavior, so the built-in collector methods are non-throwing for valid inputs and instrumentation remains side-effect-only.

## Testing

Unit tests will verify deterministic counter, histogram, gauge, and span snapshots. Endpoint tests will exercise successful provider dispatch, fallback, exhausted failure, open-circuit rejection, request-level budget denial, and cumulative API-key budget denial. Assertions will validate metric names, exact labels, span status, shared trace IDs, and non-negative durations while confirming no prompt or response content appears in span attributes.

Streaming coverage will confirm pre-first-event fallback records both provider attempts and a fallback metric. Existing tests remain the regression boundary for response payloads, route decisions, quotas, persistence, and streaming finalization. Completion requires focused observability tests, the full unittest suite, compilation, and whitespace validation.
