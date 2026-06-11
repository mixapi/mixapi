# MixAPI Follow-Up Tasks

This backlog closes the highest-priority gaps between the current implementation and the P0 acceptance criteria in `product-solutions-prd.md` and `detailed-technical-design.md`.

## Current Execution Queue

- [x] Normalize exhausted provider failures as `provider_rate_limited` (429), `upstream_timeout` (504), or `provider_unavailable` (503), while preserving route attempt records.
- [x] Enforce request-level and API-key cumulative spend budgets before provider dispatch, then reconcile reservations against actual usage.
- [x] Add in-memory provider-model circuit breakers that open after repeated transient failures and exclude open circuits from fallback attempts.
- [x] Add tenant-scoped CSV usage export with the same data isolation as the JSON usage endpoint.
- [x] Run the complete test suite, compilation checks, and whitespace validation.
- [x] Implement sticky sessions via a custom header to pin provider connections.
- [x] Implement per-account concurrency tracking to limit active requests per upstream connection.
- [x] Add account-level quotas to enforce token/request limits on provider connections.
- [x] Support automated account rotation by selecting alternative candidate connections when limits are hit.

## Deferred Follow-Ups

- [x] Replace buffered SSE with provider-native streaming and pre-first-event fallback.
- [x] Persist budget reservations and circuit state for multi-process deployments.
- [x] Add token quotas alongside the existing request quota.
- [x] Add time-window filters, pagination, and JSONL export to usage reporting.
- [x] Add admin APIs for tenant keys, model allowlists, budgets, and routing policies.
- [x] Add metrics and traces for provider latency, errors, fallbacks, circuit state, and budget denials.
- [x] Generate and validate an explicit OpenAPI contract and SDK fixtures.

## P1 Execution Queue

- [x] Add provider-neutral Draft 2020-12 structured-output validation with corrective retry, fallback, aggregate accounting, observability, and contract coverage.
- [x] Replace SQLite and in-memory runtime state with mandatory PostgreSQL/Redis, dynamic multi-vendor model mappings, versioned publication, and an idempotent legacy migration command.

## Completion Rule

A task is complete only when its focused tests pass, the full suite remains green, and its checkbox is updated in this file.
