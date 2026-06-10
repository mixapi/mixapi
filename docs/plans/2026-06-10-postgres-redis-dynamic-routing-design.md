# PostgreSQL/Redis Dynamic Routing Design

## Scope

MixAPI will replace its static provider configuration and SQLite/in-memory runtime architecture with a multi-instance control plane aligned with the relevant Sub2API scheduling pattern. PostgreSQL becomes the sole durable source of truth. Redis becomes a mandatory runtime dependency for configuration snapshots, routing coordination, circuits, quotas, budgets, and idempotency. There is no production or development mode that routes without both services.

The first release supports global provider connections, dynamic logical-model mappings, multiple independently routable vendor endpoints using the same protocol, capability-aware routing, priority and weighted selection, health tracking, circuit breaking, and fallback. Existing tenant and API-key model allowlists remain the authorization boundary over the global model catalog.

Subscription-account pooling, OAuth account rotation, sticky sessions, and per-account concurrency are not part of this release. The architecture must leave room for those features without encoding provider identity as a protocol name.

## Architecture

PostgreSQL owns durable configuration and business records. It stores provider connections, encrypted credentials, logical models, aliases, model candidates, tenant configuration, API keys, usage, route decisions, budgets, audit events, configuration versions, and a transactional configuration outbox.

Redis owns the data-plane runtime view. It stores immutable versioned configuration snapshots, the active snapshot pointer, publication locks and watermarks, circuit state, provider health, quota counters, budget reservations, and idempotency records. Requests do not query PostgreSQL to discover providers or model mappings.

Every configuration mutation writes its resource changes, audit event, configuration version, and outbox event in one PostgreSQL transaction. A background publisher consumes outbox rows, loads the complete active configuration, validates it, writes an immutable Redis snapshot, and atomically advances the active pointer. Failed publication leaves the previous snapshot active.

Each request reads the active Redis version, resolves that exact snapshot, and retains it through routing, fallback, and streaming initialization. This prevents one request from mixing candidates or credentials from different configuration versions.

## Core Domain Model

### Provider Connections

A provider connection is one independently routable upstream endpoint. Its identity is not the same as its wire protocol.

Required fields:

- Stable provider connection ID and unique operator-facing name.
- Protocol: `openai-compatible`, `anthropic`, `gemini`, or `ollama`.
- Base URL.
- AES-256-GCM encrypted credential envelope and key version.
- Credential fingerprint for operator identification.
- Request timeout.
- Active, disabled, or deleted status.
- Default priority and weight.
- Non-secret protocol metadata.
- Creation and update timestamps.

Multiple provider connections may use the same protocol. For example, an OpenRouter endpoint and an internal relay may both use `openai-compatible`, while two Gemini vendors may use separate base URLs and credentials.

### Logical Models

A logical model is a stable public model identifier such as `gpt-5.5` or `mixapi/balanced-chat`. It owns a description, status, aliases, and an ordered set of model candidates. Aliases are globally unique and resolve to one logical model before tenant authorization and route planning.

### Model Candidates

A model candidate connects one logical model to one provider connection and one upstream model ID. It contains:

- Provider connection ID and upstream model ID.
- Status, priority, and weight.
- Context and output-token limits.
- Input and output modalities.
- Tool modes and JSON Schema support.
- Streaming and embeddings support.
- Retention class and regions.
- Input and output pricing.
- Native feature flags and unsupported parameters.

The same public model can therefore route to Gemini, Anthropic, and OpenAI-compatible proxy connections without pretending those upstream model IDs or capabilities are identical.

## PostgreSQL Schema

Alembic owns schema migrations. Runtime startup verifies that the database is at the expected revision and does not create or alter tables directly.

New configuration tables:

- `provider_connections`
- `logical_models`
- `logical_model_aliases`
- `model_candidates`
- `configuration_versions`
- `configuration_outbox`
- `sqlite_migration_runs`
- `sqlite_migration_batches`

Existing SQLite-backed records move to PostgreSQL tables with equivalent constraints:

- `api_keys`
- `tenant_policies`
- `audit_events`
- `usage_events`
- `route_decisions`
- `budget_spend`

Idempotency, active budget reservations, quotas, and circuit state are runtime coordination data and move to Redis. Durable usage, settled budget spend, route history, configuration, and audit history remain in PostgreSQL.

Foreign keys use restrictive deletion. Public resources are soft deleted to preserve audit and usage attribution. Unique constraints prevent duplicate provider names, logical model IDs, aliases, and duplicate candidate mappings within one logical model.

## Credential Encryption

`MIXAPI_MASTER_KEY` is mandatory and supplies the active 256-bit key. `MIXAPI_MASTER_KEY_VERSION` identifies that key, and `MIXAPI_PREVIOUS_MASTER_KEYS` supplies older versioned keys during rotation. Provider credentials are encrypted with AES-256-GCM before PostgreSQL persistence. Each encrypted envelope stores the key version, nonce, ciphertext, and authentication tag.

Authenticated additional data binds ciphertext to the provider connection ID, protocol, and credential field name. Copying ciphertext to another provider record must fail authentication. API responses, audit events, logs, traces, Redis metadata, and migration reports never expose plaintext credentials.

Redis snapshots may contain encrypted credential envelopes, not plaintext credentials. The application decrypts credentials only while constructing version-scoped adapters in memory. Credential rotation updates the provider connection and publishes a new snapshot. Old keys remain configured during a bounded rotation window so retained snapshots can be decrypted until they expire.

## Redis Snapshot Model

The publisher writes immutable versioned keys before changing the active pointer. The snapshot includes logical models, aliases, candidates, provider metadata, encrypted credential envelopes, and a checksum over a canonical serialization.

Representative keys:

```text
mixapi:config:active_version
mixapi:config:version:{version}:manifest
mixapi:config:version:{version}:payload
mixapi:config:publish_lock
mixapi:config:outbox_watermark
```

Publication uses a Lua script that advances the active pointer only when the proposed version is not older than the current active version and the snapshot manifest is marked ready. Previous versions receive a retention TTL rather than immediate deletion so in-flight requests and controlled rollback remain possible.

Each process maintains a bounded cache of decoded immutable snapshots and version-scoped adapters. A request still reads the active Redis version before using that cache. Redis unavailability, a missing active pointer, a checksum mismatch, or an undecryptable snapshot fails closed.

## Publication Flow

Admin mutation endpoints return `202 Accepted` with the mutated resource and pending configuration version.

The publisher performs these steps:

1. Poll PostgreSQL outbox rows with `FOR UPDATE SKIP LOCKED`.
2. Acquire the Redis publication lock with a bounded lease.
3. Load all active providers, logical models, aliases, and candidates from PostgreSQL.
4. Validate references, aliases, capabilities, protocols, URLs, credentials, and candidate availability.
5. Serialize a canonical snapshot and calculate its checksum.
6. Write the new immutable version and ready manifest to Redis.
7. Atomically activate the version using Lua.
8. Mark the PostgreSQL configuration version and outbox rows published.
9. Release the publication lock.

Failures record a bounded, non-secret publication error in PostgreSQL. The previous active version remains untouched. A periodic full rebuild repairs missed events and verifies Redis contents against PostgreSQL. Operators can also request a rebuild through the admin API.

## Routing And Dispatch

Alias resolution occurs before tenant and API-key allowlist evaluation. Authorization uses the canonical logical model ID so an alias cannot bypass an allowlist.

Route planning first filters candidates by status, provider pin, endpoint, modalities, tools, schema support, region, and circuits. Eligible candidates are ordered by explicit priority and routing objective. Weight distributes requests among candidates with equal priority and comparable eligibility; selection must be deterministic for a supplied request seed so it can be tested and explained.

Adapters are selected by provider connection ID and built from that connection's protocol, base URL, decrypted credential, timeout, and metadata. Two Gemini connections therefore create two independent Gemini adapters. Fallback iterates candidates across connections, records connection ID plus protocol and upstream model, and preserves the existing normalized provider error behavior.

Circuit and health keys are scoped by provider connection ID plus upstream model ID. Protocol names alone are not unique routing identities.

## Admin API

Provider connection endpoints:

```text
POST   /admin/v1/providers
GET    /admin/v1/providers
GET    /admin/v1/providers/{provider_id}
PATCH  /admin/v1/providers/{provider_id}
DELETE /admin/v1/providers/{provider_id}
POST   /admin/v1/providers/{provider_id}/test
```

Logical model endpoints:

```text
POST   /admin/v1/models
GET    /admin/v1/models
GET    /admin/v1/models/{model_id}
PATCH  /admin/v1/models/{model_id}
DELETE /admin/v1/models/{model_id}
POST   /admin/v1/models/{model_id}/candidates
PATCH  /admin/v1/models/{model_id}/candidates/{candidate_id}
DELETE /admin/v1/models/{model_id}/candidates/{candidate_id}
```

Publication endpoints:

```text
GET  /admin/v1/configuration/status
POST /admin/v1/configuration/rebuild
```

Credentials are accepted only on provider creation or explicit rotation. Read responses expose `credential_configured`, key version, and fingerprint. Deleting a provider or candidate is a soft deletion. A mutation that would leave an active logical model without an active candidate is rejected unless the logical model is disabled in the same transaction.

All mutations append redacted audit events. Provider test calls do not publish configuration and must use bounded timeouts, SSRF-safe URL validation, and secret-free results.

## Tenancy

Provider connections, model mappings, and publication versions are global. Existing tenants continue to own API keys, model allowlists, routing defaults, budgets, usage visibility, idempotency namespace, and route history.

`/v1/models` lists the currently published logical models filtered by the effective tenant and API-key allowlist. Allowlist validation is performed against PostgreSQL configuration during admin writes and against the active snapshot during data-plane requests.

Tenant-owned provider credentials and model overrides are explicitly outside this release.

## Failure Semantics

Redis is mandatory for request authentication and routing coordination. If Redis is unavailable, the active version is absent, or the selected snapshot is invalid, public inference and model-list endpoints return a normalized `503 control_plane_unavailable` or `503 configuration_unavailable`. MixAPI does not query PostgreSQL for routing and does not use a stale process-local snapshot during an outage.

PostgreSQL is mandatory for startup, admin mutations, durable usage, settled budget spend, route history, and audit records. If PostgreSQL becomes unavailable after startup, new admin mutations fail. Inference may begin only after creating a durable request/usage intent where required by the endpoint's accounting flow. Non-streaming requests fail before upstream dispatch if the durable precondition cannot be recorded. Streaming uses a durable pre-dispatch record and final reconciliation after completion or interruption.

Provider transport failures, rate limits, timeouts, schema-validation failures, and stream initialization failures retain existing fallback behavior. Provider health updates are separate from configuration publication and never change the active snapshot.

## Mandatory Startup And Readiness

Startup requires:

- `MIXAPI_DATABASE_URL` pointing to PostgreSQL.
- `MIXAPI_REDIS_URL` pointing to Redis.
- `MIXAPI_MASTER_KEY` with a supported key version.
- PostgreSQL schema at the expected Alembic revision.
- Reachable PostgreSQL and Redis.
- A valid active Redis snapshot, or successful publication of an initial snapshot.

The application does not expose ready status until these checks pass. Liveness remains process-local. Readiness reports only bounded component states and never credentials or connection strings.

## SQLite Migration

SQLite runtime support is removed. A one-time import command preserves existing installations:

```text
mixapi migrate-sqlite \
  --sqlite-path ./mixapi.sqlite3 \
  --postgres-dsn "$MIXAPI_DATABASE_URL"
```

The command performs a dry run unless `--apply` is supplied. It imports API keys, tenant policies, audit events, usage, settled budgets, idempotency records, route decisions, and circuit history while preserving identifiers and timestamps where possible. Existing environment-based providers and the hardcoded catalog are converted into provider connections, logical models, aliases, and candidates.

Migration uses a PostgreSQL ledger and resumable batches. Re-running the same source migration is idempotent. Validation compares source and destination row counts, usage totals, budget totals, identifiers, and foreign references. Final activation publishes the initial Redis snapshot and writes a machine-readable reconciliation report.

The old deployment must be stopped and its SQLite file treated as read-only during final migration. There is no dual-write or live replication mode.

## Testing

Tests run against real PostgreSQL and Redis services. In-memory fakes may exist only for narrow unit tests that do not claim persistence, locking, publication, encryption, or multi-instance behavior.

Required integration coverage includes:

- Alembic migration from an empty PostgreSQL database.
- Provider, logical model, alias, and candidate CRUD.
- AES-GCM encryption, redaction, decryption, and key rotation.
- Transactional resource, audit, configuration version, and outbox writes.
- Concurrent outbox workers using `SKIP LOCKED`.
- Atomic Redis activation and version rollback prevention.
- Checksum and credential failures preserving the active version.
- Multiple provider connections using one protocol.
- `gpt-5.5` routing to Gemini, Anthropic, and OpenAI-compatible candidates.
- Capability filtering, priority, weighting, circuits, and fallback.
- One request retaining one snapshot version through fallback and streaming initialization.
- Redis outage failing closed without PostgreSQL route fallback.
- Multi-instance consistency.
- Complete, resumable, and idempotent SQLite migration.
- OpenAPI and SDK fixture coverage for every new admin operation.

CI starts PostgreSQL and Redis service containers, applies Alembic migrations, publishes a test snapshot, and runs the complete suite.

## Delivery Sequence

1. Add mandatory PostgreSQL, Redis, Alembic, encryption, and local container infrastructure.
2. Move durable control-plane and reporting stores from SQLite to PostgreSQL.
3. Move runtime coordination stores to Redis.
4. Add dynamic providers, logical models, aliases, candidates, audit, and outbox writes.
5. Add versioned snapshot publication and fail-closed snapshot loading.
6. Refactor routing and adapter construction around provider connection IDs and snapshot versions.
7. Add health checks, weighted routing, and the dynamic admin API contract.
8. Add the SQLite migration command and reconciliation report.
9. Remove SQLite and static provider/catalog runtime paths.

The refactor is complete only when deployed application startup requires PostgreSQL and Redis, all data-plane routing comes from a published Redis snapshot, and no environment-specific base URL or hardcoded catalog path remains in runtime selection.
