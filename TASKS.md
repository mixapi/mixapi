# MixAPI Follow-Up Tasks

This backlog closes the highest-priority gaps between the current implementation and the P0 acceptance criteria in `product-solutions-prd.md` and `detailed-technical-design.md`.

## Current Execution Queue

- [x] Normalize exhausted provider failures as `provider_rate_limited` (429), `upstream_timeout` (504), or `provider_unavailable` (503), while preserving route attempt records.
- [x] Enforce request-level and API-key cumulative spend budgets before provider dispatch, then reconcile reservations against actual usage.
- [x] Add in-memory provider-model circuit breakers that open after repeated transient failures and exclude open circuits from fallback attempts.
- [x] Add tenant-scoped CSV usage export with the same data isolation as the JSON usage endpoint.
- [x] Run the complete test suite, compilation checks, and whitespace validation.

## Deferred Follow-Ups

- [x] Replace buffered SSE with provider-native streaming and pre-first-event fallback.
- [x] Persist budget reservations and circuit state for multi-process deployments.
- [x] Add token quotas alongside the existing request quota.
- [ ] Add time-window filters, pagination, and JSONL export to usage reporting.
- [ ] Add admin APIs for tenant keys, model allowlists, budgets, and routing policies.
- [ ] Add metrics and traces for provider latency, errors, fallbacks, circuit state, and budget denials.
- [ ] Generate and validate an explicit OpenAPI contract and SDK fixtures.

## Completion Rule

A task is complete only when its focused tests pass, the full suite remains green, and its checkbox is updated in this file.
