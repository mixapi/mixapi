# PostgreSQL/Redis Dynamic Routing Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace MixAPI's SQLite, in-memory runtime stores, static catalog, and single-endpoint provider configuration with mandatory PostgreSQL/Redis infrastructure, dynamic multi-vendor model mappings, versioned runtime snapshots, and an idempotent SQLite migration tool.

**Architecture:** PostgreSQL is the only durable source of truth and records configuration changes through a transactional outbox. Redis is mandatory for versioned routing snapshots and runtime coordination; requests fail closed when Redis cannot provide the active snapshot. Provider connections are distinct from provider protocols, allowing one logical model such as `gpt-5.5` to route across multiple Gemini, Anthropic, or OpenAI-compatible vendor endpoints.

**Tech Stack:** Python 3.14, FastAPI, Pydantic, psycopg 3 with connection pools, PostgreSQL 17, Redis 8, redis-py, Alembic, SQLAlchemy Core metadata for migrations only, cryptography AES-GCM, pytest, Docker Compose.

**Design:** `docs/plans/2026-06-10-postgres-redis-dynamic-routing-design.md`

**Execution rules:** Use `@superpowers:test-driven-development` for each behavior change. Use `@superpowers:systematic-debugging` for unexpected failures. Before claiming completion, use `@superpowers:verification-before-completion` and run the complete verification block in Task 18.

### Task 1: Add Mandatory PostgreSQL/Redis Development Infrastructure

**Files:**
- Modify: `pyproject.toml`
- Create: `compose.yaml`
- Create: `.env.example`
- Create: `scripts/wait_for_dependencies.py`
- Create: `tests/integration/test_dependencies.py`
- Create: `tests/conftest.py`

**Step 1: Write the failing dependency smoke test**

Create `tests/integration/test_dependencies.py` with tests that connect using `MIXAPI_DATABASE_URL` and `MIXAPI_REDIS_URL`, execute `SELECT 1`, and call Redis `PING`. Do not skip when variables are absent; raise an assertion explaining that dependencies are mandatory.

```python
def test_postgres_is_required(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)


def test_redis_is_required(redis_url: str) -> None:
    assert redis.Redis.from_url(redis_url).ping() is True
```

**Step 2: Run the test and verify it fails**

Run: `python -m pytest tests/integration/test_dependencies.py -v`

Expected: FAIL because psycopg/redis are not installed and mandatory test URLs are not configured.

**Step 3: Add dependencies and service containers**

Add these project dependencies:

```toml
"alembic>=1.16.0",
"cryptography>=45.0.0",
"psycopg[binary,pool]>=3.2.0",
"redis>=6.0.0",
"sqlalchemy>=2.0.0",
```

Add a `test` optional dependency containing `pytest>=9.0.0`. Add PostgreSQL and Redis health-checked services to `compose.yaml`, with a dedicated `mixapi_test` database and non-default host ports. Set deterministic test URLs in `.env.example`.

**Step 4: Add mandatory pytest fixtures**

In `tests/conftest.py`, expose `database_url` and `redis_url` fixtures from environment variables. Add a session fixture that waits for both services. Do not add SQLite or in-memory fallback fixtures.

**Step 5: Start services and verify the test passes**

Run:

```bash
docker compose up -d postgres redis
python scripts/wait_for_dependencies.py
python -m pytest tests/integration/test_dependencies.py -v
```

Expected: 2 passed.

**Step 6: Commit**

```bash
git add pyproject.toml compose.yaml .env.example scripts/wait_for_dependencies.py tests/conftest.py tests/integration/test_dependencies.py
git commit -m "build: require postgres and redis test services"
```

### Task 2: Add Settings, Connection Pools, and Mandatory Startup Checks

**Files:**
- Create: `mixapi/settings.py`
- Create: `mixapi/postgres.py`
- Create: `mixapi/redis_runtime.py`
- Modify: `mixapi/errors.py`
- Modify: `mixapi/app.py`
- Modify: `scripts/generate_openapi.py`
- Test: `tests/integration/test_startup_dependencies.py`

**Step 1: Write failing startup tests**

Cover these cases:

- Missing `MIXAPI_DATABASE_URL`, `MIXAPI_REDIS_URL`, or `MIXAPI_MASTER_KEY` raises `ConfigurationError` before app creation.
- Unreachable PostgreSQL prevents readiness.
- Unreachable Redis prevents readiness.
- Healthy services create pools once and close them during FastAPI lifespan shutdown.

Use an explicit immutable settings object in tests:

```python
settings = Settings(
    database_url=database_url,
    redis_url=redis_url,
    master_key=bytes.fromhex("00" * 32),
    master_key_version=1,
    previous_master_keys={},
    admin_api_key="admin-secret",
)
```

**Step 2: Run the focused test**

Run: `python -m pytest tests/integration/test_startup_dependencies.py -v`

Expected: FAIL because settings and mandatory lifecycle checks do not exist.

**Step 3: Implement settings and clients**

`mixapi/settings.py` must parse:

- `MIXAPI_DATABASE_URL`
- `MIXAPI_REDIS_URL`
- `MIXAPI_MASTER_KEY` as the active base64-encoded 32-byte key
- `MIXAPI_MASTER_KEY_VERSION` as the active positive integer version
- `MIXAPI_PREVIOUS_MASTER_KEYS` as optional `version:base64key` comma-separated values
- `MIXAPI_ADMIN_KEY`
- pool sizes, timeouts, snapshot retention, idempotency TTL, and publisher intervals

`mixapi/postgres.py` provides a `psycopg_pool.ConnectionPool` and transaction context manager. `mixapi/redis_runtime.py` provides one configured Redis client and maps Redis connection errors to a typed runtime exception.

**Step 4: Add application lifespan checks**

Change the app factory signature to dependency injection rather than dozens of provider arguments:

```python
def create_app(
    settings: Settings | None = None,
    *,
    adapter_factory: AdapterFactory | None = None,
    observability: Observability | None = None,
) -> FastAPI:
    ...
```

Remove module-level `app = create_app()`. Deployment uses `uvicorn mixapi.app:create_app --factory`. The lifespan opens both pools, verifies PostgreSQL and Redis, checks schema/snapshot readiness in later tasks, and closes resources.

**Step 5: Add normalized dependency errors**

Add `control_plane_unavailable()` and `configuration_unavailable()` helpers returning non-secret `503` envelopes.

**Step 6: Re-run focused tests**

Run: `python -m pytest tests/integration/test_startup_dependencies.py -v`

Expected: PASS.

**Step 7: Commit**

```bash
git add mixapi/settings.py mixapi/postgres.py mixapi/redis_runtime.py mixapi/errors.py mixapi/app.py scripts/generate_openapi.py tests/integration/test_startup_dependencies.py
git commit -m "feat: require postgres redis and master key at startup"
```

### Task 3: Introduce Alembic and the Complete PostgreSQL Schema

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/20260610_0001_initial_postgres.py`
- Create: `mixapi/schema.py`
- Create: `scripts/migrate.py`
- Test: `tests/integration/test_postgres_schema.py`

**Step 1: Write the failing schema test**

The test creates a fresh temporary PostgreSQL schema, runs `alembic upgrade head`, and asserts that all required tables and constraints exist. Include:

```text
api_keys, tenant_policies, audit_events, usage_events, route_decisions,
budget_spend, provider_connections, logical_models, logical_model_aliases,
model_candidates, configuration_versions, configuration_outbox,
usage_write_intents, budget_reconciliation_outbox,
sqlite_migration_runs, sqlite_migration_batches
```

Also assert PostgreSQL-native types where required: `JSONB`, `TIMESTAMPTZ`, `NUMERIC(20,8)`, and `BIGSERIAL`/identity versions.

**Step 2: Run the schema test**

Run: `python -m pytest tests/integration/test_postgres_schema.py -v`

Expected: FAIL because Alembic configuration is absent.

**Step 3: Define metadata and migration**

Use SQLAlchemy Core metadata only to make constraints and Alembic inspection deterministic. Runtime repositories continue using psycopg and explicit SQL. Include foreign keys, restrictive deletes, soft-delete timestamps, status checks, unique aliases, and indexes for tenant/time usage queries and unprocessed outbox rows.

The outbox tables include claim leases:

```text
claimed_by, claimed_at, claim_expires_at, processed_at, attempts, last_error
```

**Step 4: Make migration execution explicit**

`scripts/migrate.py` runs Alembic using `MIXAPI_DATABASE_URL`. Application startup verifies the current revision but never executes migrations automatically.

**Step 5: Run upgrade and downgrade verification**

Run:

```bash
python scripts/migrate.py upgrade head
python -m pytest tests/integration/test_postgres_schema.py -v
alembic downgrade base
alembic upgrade head
```

Expected: PASS and both migration directions complete without errors.

**Step 6: Commit**

```bash
git add alembic.ini alembic mixapi/schema.py scripts/migrate.py tests/integration/test_postgres_schema.py
git commit -m "feat: add postgres schema migrations"
```

### Task 4: Implement Versioned AES-GCM Credential Encryption

**Files:**
- Create: `mixapi/secrets.py`
- Test: `tests/test_secrets.py`

**Step 1: Write failing encryption tests**

Test round-trip encryption, random nonces, wrong-key rejection, additional-data binding, redacted public envelopes, and old-key decryption during rotation.

```python
aad = CredentialAAD(provider_id="provider_1", protocol="gemini", field="api_key")
envelope = cipher.encrypt("secret", aad)
assert cipher.decrypt(envelope, aad) == "secret"
with pytest.raises(InvalidCredentialCiphertext):
    cipher.decrypt(envelope, replace(aad, provider_id="provider_2"))
```

**Step 2: Run the test**

Run: `python -m pytest tests/test_secrets.py -v`

Expected: FAIL because `mixapi.secrets` does not exist.

**Step 3: Implement the cipher**

Create immutable `EncryptedCredential` and `CredentialAAD` records. Serialize envelope fields as version, base64 nonce, and base64 ciphertext including the GCM tag. Expose only a SHA-256 fingerprint prefix in public records. Never implement plaintext serialization or `repr` output.

**Step 4: Run the test**

Run: `python -m pytest tests/test_secrets.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add mixapi/secrets.py tests/test_secrets.py
git commit -m "feat: encrypt provider credentials with versioned aes gcm"
```

### Task 5: Add Dynamic Configuration Domain Records and PostgreSQL Repository

**Files:**
- Create: `mixapi/configuration.py`
- Create: `mixapi/repositories/__init__.py`
- Create: `mixapi/repositories/configuration.py`
- Modify: `mixapi/models.py`
- Test: `tests/integration/test_configuration_repository.py`

**Step 1: Write failing repository tests**

Cover provider connection creation, credential rotation, soft deletion, logical-model creation, globally unique aliases, candidate creation, candidate capabilities, and transactional audit/version/outbox writes.

For every mutation, assert in one database snapshot:

```python
assert resource.updated_at == version.created_at
assert audit.target_id == resource.id
assert outbox.configuration_version == version.version
assert outbox.processed_at is None
```

Simulate a unique-alias conflict and verify the resource, audit, version, and outbox writes all roll back.

**Step 2: Run the test**

Run: `python -m pytest tests/integration/test_configuration_repository.py -v`

Expected: FAIL because repository records do not exist.

**Step 3: Define the records**

Add immutable records:

```python
ProviderConnection
LogicalModel
LogicalModelAlias
ModelCandidate
ConfigurationVersion
ConfigurationOutboxEvent
ConfigurationMutationResult[T]
```

Extend `ProviderModel` with `provider_connection_id`, `priority`, and `weight`. Keep `provider` as protocol-facing metadata only until Task 14 updates response naming.

**Step 4: Implement explicit SQL repository methods**

Required methods include create/list/get/update/soft-delete for providers and logical models, create/update/delete candidates, load the complete active configuration, claim outbox rows with `FOR UPDATE SKIP LOCKED`, renew/complete/release claims, and record publication failure.

Validate URLs using a shared URL validator that rejects credentials in URLs, fragments, unsupported schemes, loopback/private addresses unless explicitly allowlisted, and overlong values.

**Step 5: Re-run focused tests**

Run: `python -m pytest tests/integration/test_configuration_repository.py -v`

Expected: PASS.

**Step 6: Commit**

```bash
git add mixapi/configuration.py mixapi/repositories mixapi/models.py tests/integration/test_configuration_repository.py
git commit -m "feat: persist dynamic providers and model mappings"
```

### Task 6: Move API Keys, Tenant Policies, and Audits to PostgreSQL

**Files:**
- Modify: `mixapi/control_plane.py`
- Create: `mixapi/repositories/control_plane.py`
- Create: `mixapi/auth_cache.py`
- Modify: `mixapi/auth.py`
- Modify: `mixapi/app.py`
- Test: `tests/integration/test_postgres_control_plane.py`
- Modify: `tests/test_admin_api.py`
- Delete: in-memory control-plane implementation from `mixapi/control_plane.py`

**Step 1: Port current control-plane behavior tests**

Convert persistence claims to PostgreSQL integration tests: secret returned once, hash-only persistence, revocation, expiry, tenant/key allowlist intersection, routing defaults, audit redaction, and atomic concurrent mutation/audit writes.

Add Redis-backed auth cache tests:

- Cache miss reads PostgreSQL and populates Redis.
- Cache hit does not query PostgreSQL.
- Revocation evicts the cache immediately.
- Redis unavailable returns `503 control_plane_unavailable`; it does not authenticate from PostgreSQL alone.

**Step 2: Run focused tests**

Run: `python -m pytest tests/integration/test_postgres_control_plane.py tests/test_admin_api.py -v`

Expected: FAIL while the application still selects SQLite/in-memory stores.

**Step 3: Implement PostgreSQL control-plane repository**

Preserve the `ControlPlaneStore` protocol where useful, but make `PostgresControlPlaneStore` the only runtime implementation. Use database transactions for mutations and audit insertion. Store API-key hashes as binary or fixed lowercase hex with a unique index.

**Step 4: Implement Redis auth caching**

Cache managed key records by key hash with bounded TTL. Store only the fields needed to construct `Principal`; never cache plaintext keys. Use versioned JSON and explicit decoding validation.

**Step 5: Remove runtime in-memory selection**

Delete `InMemoryControlPlaneStore` from application wiring. Tests that require a fake must define it inside the test module rather than shipping it as a runtime option.

**Step 6: Run focused tests**

Run: `python -m pytest tests/integration/test_postgres_control_plane.py tests/test_admin_api.py -v`

Expected: PASS.

**Step 7: Commit**

```bash
git add mixapi/control_plane.py mixapi/repositories/control_plane.py mixapi/auth_cache.py mixapi/auth.py mixapi/app.py tests/integration/test_postgres_control_plane.py tests/test_admin_api.py
git commit -m "refactor: move tenant control plane to postgres"
```

### Task 7: Move Usage, Route Decisions, and Settled Spend to PostgreSQL

**Files:**
- Create: `mixapi/repositories/usage.py`
- Create: `mixapi/repositories/route_decisions.py`
- Create: `mixapi/repositories/budgets.py`
- Modify: `mixapi/usage.py`
- Modify: `mixapi/route_decisions.py`
- Modify: `mixapi/budget.py`
- Modify: `mixapi/app.py`
- Test: `tests/integration/test_postgres_accounting.py`
- Modify: `tests/test_usage_endpoint.py`
- Modify: `tests/test_route_decisions.py`

**Step 1: Write failing PostgreSQL accounting tests**

Cover tenant-scoped usage pagination, inclusive timestamp bounds, route persistence, concurrent settled-spend increments, and atomic usage plus spend reconciliation. Add a pre-dispatch `usage_write_intents` test proving PostgreSQL failure prevents upstream dispatch.

**Step 2: Run focused tests**

Run: `python -m pytest tests/integration/test_postgres_accounting.py tests/test_usage_endpoint.py tests/test_route_decisions.py -v`

Expected: FAIL because SQLite/in-memory stores remain wired.

**Step 3: Implement PostgreSQL repositories**

Use keyset pagination over `(created_at, id)`. Write usage events, settled spend, reconciliation outbox rows, and intent completion in one transaction. Preserve decimal precision using PostgreSQL `NUMERIC` and Python `Decimal` throughout.

**Step 4: Add durable streaming intents**

Before opening an upstream stream, insert a durable intent containing request ID, tenant, API key, endpoint, logical model, snapshot version, and reservation IDs. On completion or interruption, reconcile it exactly once. A recovery worker can later finalize abandoned intents.

**Step 5: Remove SQLite/in-memory accounting wiring**

Application routes must instantiate only PostgreSQL usage, route, and durable budget repositories.

**Step 6: Run focused tests**

Run: `python -m pytest tests/integration/test_postgres_accounting.py tests/test_usage_endpoint.py tests/test_route_decisions.py -v`

Expected: PASS.

**Step 7: Commit**

```bash
git add mixapi/repositories/usage.py mixapi/repositories/route_decisions.py mixapi/repositories/budgets.py mixapi/usage.py mixapi/route_decisions.py mixapi/budget.py mixapi/app.py tests/integration/test_postgres_accounting.py tests/test_usage_endpoint.py tests/test_route_decisions.py
git commit -m "refactor: persist accounting and routes in postgres"
```

### Task 8: Move Runtime Coordination to Redis With Lua Atomicity

**Files:**
- Create: `mixapi/redis_scripts.py`
- Create: `mixapi/runtime/__init__.py`
- Create: `mixapi/runtime/idempotency.py`
- Create: `mixapi/runtime/quota.py`
- Create: `mixapi/runtime/budget.py`
- Create: `mixapi/runtime/circuits.py`
- Modify: `mixapi/idempotency.py`
- Modify: `mixapi/quota.py`
- Modify: `mixapi/circuits.py`
- Modify: `mixapi/budget.py`
- Test: `tests/integration/test_redis_runtime.py`

**Step 1: Write failing Redis concurrency tests**

Use two independent Redis clients and application service instances. Cover:

- Idempotency first-write-wins with body-hash conflict detection and TTL.
- Atomic request/token quota reservation and reconciliation.
- Atomic budget reservation without oversubscription.
- Idempotent budget reconciliation keyed by reservation ID.
- Circuit failures shared across processes and recovery timeout.
- Provider health scoped by provider connection ID and upstream model.
- Redis outage raising the typed fail-closed error.

**Step 2: Run focused tests**

Run: `python -m pytest tests/integration/test_redis_runtime.py -v`

Expected: FAIL because Redis runtime services do not exist.

**Step 3: Implement versioned Redis key schemas**

Prefix every key with a configurable namespace and record format version. Scope tenant data by tenant/API-key IDs and provider state by provider connection ID plus upstream model ID.

**Step 4: Implement Lua scripts**

Use Lua for compare-and-set idempotency, quota reservation/reconciliation, budget reservation/reconciliation, and circuit transitions. Load scripts by SHA and transparently recover from `NOSCRIPT`.

Budget reconciliation updates Redis immediately and writes durable PostgreSQL settlement in Task 7. If PostgreSQL settlement succeeds but Redis update fails, `budget_reconciliation_outbox` repairs Redis idempotently.

**Step 5: Delete runtime in-memory implementations**

Keep protocols and domain records where they clarify contracts. Remove in-memory implementations from production modules.

**Step 6: Run focused tests**

Run: `python -m pytest tests/integration/test_redis_runtime.py -v`

Expected: PASS.

**Step 7: Commit**

```bash
git add mixapi/redis_scripts.py mixapi/runtime mixapi/idempotency.py mixapi/quota.py mixapi/circuits.py mixapi/budget.py tests/integration/test_redis_runtime.py
git commit -m "refactor: coordinate gateway runtime through redis"
```

### Task 9: Implement Canonical Configuration Snapshots

**Files:**
- Create: `mixapi/snapshots.py`
- Create: `mixapi/runtime/snapshots.py`
- Test: `tests/test_snapshots.py`
- Test: `tests/integration/test_redis_snapshots.py`

**Step 1: Write failing serialization tests**

Test deterministic canonical JSON, checksum stability, strict schema versioning, alias lookup, complete provider/candidate reconstruction, encrypted credential preservation, and rejection of unknown fields or invalid checksums.

**Step 2: Write failing Redis activation tests**

Cover immutable version keys, ready manifests, atomic active-pointer advancement, prevention of version rollback, previous-version TTL, missing payload behavior, and concurrent publishers.

**Step 3: Run focused tests**

Run: `python -m pytest tests/test_snapshots.py tests/integration/test_redis_snapshots.py -v`

Expected: FAIL because snapshot services do not exist.

**Step 4: Implement snapshot records and codec**

Create `ConfigurationSnapshot` with version, generated timestamp, providers, canonical logical models, aliases, and checksum. Canonical serialization uses sorted keys and compact separators; decimals remain strings.

**Step 5: Implement Redis snapshot store**

Methods:

```python
write_pending(snapshot)
mark_ready(version, checksum)
activate(version, checksum)
active_version()
load(version)
retain_versions(active_version, count, ttl_seconds)
```

Activation is one Lua script checking ready state, checksum, and monotonic version.

**Step 6: Run focused tests**

Run: `python -m pytest tests/test_snapshots.py tests/integration/test_redis_snapshots.py -v`

Expected: PASS.

**Step 7: Commit**

```bash
git add mixapi/snapshots.py mixapi/runtime/snapshots.py tests/test_snapshots.py tests/integration/test_redis_snapshots.py
git commit -m "feat: add versioned redis configuration snapshots"
```

### Task 10: Add Transactional Outbox Publication and Recovery Workers

**Files:**
- Create: `mixapi/publication.py`
- Create: `mixapi/workers.py`
- Modify: `mixapi/app.py`
- Test: `tests/integration/test_configuration_publication.py`

**Step 1: Write failing publisher tests**

Cover:

- Two workers claim disjoint rows with `SKIP LOCKED`.
- Expired claims are recoverable.
- A complete configuration publishes and marks its version active.
- Invalid aliases, missing candidates, bad URLs, unknown protocols, or undecryptable credentials mark publication failed.
- Failed publication preserves the old Redis active version.
- Duplicate events for one version produce one snapshot.
- Periodic full rebuild repairs a deleted Redis snapshot.

**Step 2: Run focused tests**

Run: `python -m pytest tests/integration/test_configuration_publication.py -v`

Expected: FAIL because no publisher exists.

**Step 3: Implement validation and publication**

`ConfigurationPublisher.publish(version)` loads a repeatable-read PostgreSQL view, validates the complete graph, creates one canonical snapshot, writes it to Redis, atomically activates it, and only then marks PostgreSQL version/outbox rows published.

Publication errors store a bounded code and message without URL credentials, API keys, ciphertext, or provider responses.

**Step 4: Add managed workers**

Create background loops for configuration outbox publication, full rebuild, expired usage intents, and budget reconciliation. Start and stop them through FastAPI lifespan. Use instance IDs and bounded shutdown waits.

**Step 5: Run focused tests**

Run: `python -m pytest tests/integration/test_configuration_publication.py -v`

Expected: PASS.

**Step 6: Commit**

```bash
git add mixapi/publication.py mixapi/workers.py mixapi/app.py tests/integration/test_configuration_publication.py
git commit -m "feat: publish configuration through transactional outbox"
```

### Task 11: Seed the Initial Dynamic Catalog and Enforce Readiness

**Files:**
- Create: `mixapi/bootstrap.py`
- Create: `scripts/bootstrap_configuration.py`
- Modify: `mixapi/app.py`
- Modify: `mixapi/catalog.py`
- Test: `tests/integration/test_bootstrap_readiness.py`

**Step 1: Write failing bootstrap tests**

Test an empty database, idempotent seed, initial snapshot publication, invalid seed rejection, and readiness behavior when no active snapshot exists.

The seed payload is explicit JSON/YAML, not environment-specific base URL arguments embedded in `create_app`.

**Step 2: Run the tests**

Run: `python -m pytest tests/integration/test_bootstrap_readiness.py -v`

Expected: FAIL because startup still relies on `default_catalog()`.

**Step 3: Implement bootstrap import**

`scripts/bootstrap_configuration.py` reads a configuration document, encrypts credentials, writes provider/model/candidate mutations, waits for publication, and exits non-zero if publication fails. Re-running the same IDs updates only changed records and produces a new version only when content changes.

**Step 4: Enforce readiness**

Add `/health/live` and `/health/ready`. Readiness requires PostgreSQL revision, Redis connectivity, master-key availability, and a valid active snapshot. Public routes must not become available before readiness.

**Step 5: Deprecate static catalog runtime use**

Keep `mixapi/catalog.py` only as a migration/bootstrap fixture until Task 17. No request route may call `default_catalog()`.

**Step 6: Run focused tests**

Run: `python -m pytest tests/integration/test_bootstrap_readiness.py -v`

Expected: PASS.

**Step 7: Commit**

```bash
git add mixapi/bootstrap.py scripts/bootstrap_configuration.py mixapi/app.py mixapi/catalog.py tests/integration/test_bootstrap_readiness.py
git commit -m "feat: bootstrap and require a published model catalog"
```

### Task 12: Add Provider Connection Admin APIs

**Files:**
- Create: `mixapi/admin/__init__.py`
- Create: `mixapi/admin/providers.py`
- Create: `mixapi/provider_testing.py`
- Modify: `mixapi/app.py`
- Test: `tests/integration/test_provider_admin_api.py`

**Step 1: Write failing endpoint tests**

Cover create/list/get/update/delete/credential rotation, protocol validation, SSRF-safe URL validation, secret redaction, duplicate names, optimistic update conflicts, `202` pending version responses, audit rows, and soft deletion.

Provider test coverage must verify bounded timeout and protocol-specific request construction against local fake HTTP upstreams. Test results expose status, latency, and normalized error class only.

**Step 2: Run focused tests**

Run: `python -m pytest tests/integration/test_provider_admin_api.py -v`

Expected: FAIL because routes do not exist.

**Step 3: Implement provider request parsing and service methods**

Provider create accepts:

```json
{
  "name": "vendor-gemini-a",
  "protocol": "gemini",
  "base_url": "https://vendor.example",
  "credential": "secret",
  "timeout_seconds": 30,
  "priority": 100,
  "weight": 1,
  "metadata": {}
}
```

Credential rotation is explicit through a credential field on PATCH and never returns plaintext.

**Step 4: Run focused tests**

Run: `python -m pytest tests/integration/test_provider_admin_api.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add mixapi/admin mixapi/provider_testing.py mixapi/app.py tests/integration/test_provider_admin_api.py
git commit -m "feat: manage provider connections dynamically"
```

### Task 13: Add Logical Model, Alias, Candidate, and Publication APIs

**Files:**
- Create: `mixapi/admin/models.py`
- Create: `mixapi/admin/publication.py`
- Modify: `mixapi/app.py`
- Test: `tests/integration/test_model_admin_api.py`
- Test: `tests/integration/test_publication_admin_api.py`

**Step 1: Write failing logical-model tests**

Cover logical model CRUD, alias uniqueness, candidate CRUD, capability validation, provider reference validation, soft deletion, and rejection of mutations that leave an active model without candidates.

Create an end-to-end fixture where public `gpt-5.5` maps to:

- Gemini connection A / `gemini-2.5-pro`
- Anthropic connection B / `claude-sonnet-4`
- OpenAI-compatible proxy C / `vendor/gpt-compatible`

**Step 2: Write failing publication API tests**

Cover status lookup, pending/published/failed fields, redacted errors, manual rebuild, rebuild locking, and `202 Accepted` responses.

**Step 3: Run focused tests**

Run: `python -m pytest tests/integration/test_model_admin_api.py tests/integration/test_publication_admin_api.py -v`

Expected: FAIL because routes do not exist.

**Step 4: Implement model and publication routes**

Use repository transaction methods from Task 5. Return canonical model IDs and aliases separately. Candidate API payloads expose provider connection IDs, never encrypted credentials.

**Step 5: Run focused tests**

Run: `python -m pytest tests/integration/test_model_admin_api.py tests/integration/test_publication_admin_api.py -v`

Expected: PASS.

**Step 6: Commit**

```bash
git add mixapi/admin/models.py mixapi/admin/publication.py mixapi/app.py tests/integration/test_model_admin_api.py tests/integration/test_publication_admin_api.py
git commit -m "feat: manage logical models and candidates dynamically"
```

### Task 14: Build Version-Scoped Adapters and Multi-Vendor Routing

**Files:**
- Create: `mixapi/adapter_factory.py`
- Modify: `mixapi/adapters.py`
- Modify: `mixapi/routing.py`
- Modify: `mixapi/models.py`
- Modify: `mixapi/app.py`
- Test: `tests/test_routing.py`
- Test: `tests/integration/test_dynamic_routing.py`

**Step 1: Write failing unit routing tests**

Cover alias resolution, canonical allowlist enforcement, priority, objective scoring, deterministic weighted rendezvous selection, provider connection pinning, capability rejection, disabled providers, and fallback ordering.

Pass the request ID as the selection seed:

```python
decision = plan_route(
    snapshot,
    request_body,
    endpoint="responses",
    selection_seed="req_123",
    model_allowlist=("gpt-5.5",),
)
```

For `balanced`, choose among the lowest-priority eligible set using weighted rendezvous hashing. For explicit cost/latency/reliability objectives, select the best metric bucket and use weight only to break equivalent candidates.

**Step 2: Write failing dynamic dispatch tests**

Use three fake upstream servers. Verify one public model dispatches to different base URLs across seeds, provider connection A failure falls back to B, two Gemini connections use separate credentials, and one request keeps the same snapshot version after a newer version activates.

**Step 3: Run focused tests**

Run: `python -m pytest tests/test_routing.py tests/integration/test_dynamic_routing.py -v`

Expected: FAIL because adapters are keyed by protocol and catalog is static.

**Step 4: Implement the adapter factory**

`AdapterFactory.for_connection(snapshot_version, provider_connection)` decrypts credentials and constructs the existing protocol adapter with that connection's base URL and timeout. Cache adapters by `(snapshot_version, provider_connection_id, updated_at)` and evict them with the snapshot cache.

Remove `CompositeProviderAdapter`'s `dict[protocol, adapter]` selection. Dispatch receives an adapter resolved for the candidate's provider connection.

**Step 5: Refactor route metadata**

Persist and expose provider connection ID/name, protocol, and upstream model separately. Keep the existing `provider` response field as the stable connection name; add protocol in route metadata rather than returning ambiguous `gemini` for all Gemini vendors.

**Step 6: Run focused tests**

Run: `python -m pytest tests/test_routing.py tests/integration/test_dynamic_routing.py tests/test_fallbacks.py tests/test_streaming.py -v`

Expected: PASS.

**Step 7: Commit**

```bash
git add mixapi/adapter_factory.py mixapi/adapters.py mixapi/routing.py mixapi/models.py mixapi/app.py tests/test_routing.py tests/integration/test_dynamic_routing.py tests/test_fallbacks.py tests/test_streaming.py
git commit -m "refactor: route models across dynamic provider connections"
```

### Task 15: Enforce Snapshot Consistency and Redis Fail-Closed Behavior End to End

**Files:**
- Create: `mixapi/runtime/context.py`
- Modify: `mixapi/app.py`
- Modify: `mixapi/streaming.py`
- Modify: `mixapi/observability.py`
- Test: `tests/integration/test_snapshot_request_consistency.py`
- Test: `tests/integration/test_redis_fail_closed.py`
- Modify: `tests/test_observability.py`

**Step 1: Write failing request-context tests**

Assert that model listing, route planning, budget estimation, dispatch, fallback, usage, route decisions, and streaming completion all record one snapshot version. Activate a newer snapshot during dispatch and verify the in-flight request remains on the original version.

**Step 2: Write failing outage tests**

Stop Redis after app startup and verify `/v1/models`, `/v1/responses`, `/v1/embeddings`, and managed-key authentication return normalized `503` errors. Assert no PostgreSQL model query and no provider dispatch occurs. Restart Redis and verify recovery without process restart.

**Step 3: Run focused tests**

Run: `python -m pytest tests/integration/test_snapshot_request_consistency.py tests/integration/test_redis_fail_closed.py -v`

Expected: FAIL until a request-scoped snapshot context is used everywhere.

**Step 4: Implement request runtime context**

Create an immutable context containing request ID, trace ID, active snapshot version, decoded snapshot, and version-scoped adapter resolver. Resolve it once after authentication and pass it through endpoint helpers and stream finalizers.

**Step 5: Extend telemetry**

Add safe labels/attributes for snapshot version, provider connection ID, protocol, candidate ID, publication failures, and Redis dependency failures. Never label credentials, base URLs, model input, or output.

**Step 6: Run focused tests**

Run: `python -m pytest tests/integration/test_snapshot_request_consistency.py tests/integration/test_redis_fail_closed.py tests/test_observability.py -v`

Expected: PASS.

**Step 7: Commit**

```bash
git add mixapi/runtime/context.py mixapi/app.py mixapi/streaming.py mixapi/observability.py tests/integration/test_snapshot_request_consistency.py tests/integration/test_redis_fail_closed.py tests/test_observability.py
git commit -m "feat: pin requests to redis configuration versions"
```

### Task 16: Publish the Dynamic Admin and Route Contracts

**Files:**
- Modify: `mixapi/api_contract.py`
- Modify: `openapi/openapi.json`
- Modify: `sdk-fixtures/manifest.json`
- Create: `sdk-fixtures/requests/provider-create.json`
- Create: `sdk-fixtures/requests/provider-update.json`
- Create: `sdk-fixtures/requests/logical-model-create.json`
- Create: `sdk-fixtures/requests/model-candidate-create.json`
- Create: `sdk-fixtures/responses/provider-record.json`
- Create: `sdk-fixtures/responses/logical-model-record.json`
- Create: `sdk-fixtures/responses/configuration-status.json`
- Modify: existing response and route fixtures for provider connection metadata
- Modify: `tests/test_openapi_contract.py`
- Modify: `tests/test_sdk_fixtures.py`

**Step 1: Add failing contract assertions**

Require stable operation IDs for every provider/model/candidate/publication endpoint, explicit request/response models, `202` mutation responses, provider credential redaction, and route metadata fields for snapshot version and provider connection.

**Step 2: Run contract tests**

Run: `python -m pytest tests/test_openapi_contract.py tests/test_sdk_fixtures.py -v`

Expected: FAIL because operations and fixtures are missing.

**Step 3: Add contract models and fixtures**

Extend `CONTRACT_MODEL_TYPES` and `OPERATION_CONTRACTS`. Ensure every JSON operation model has at least one manifest fixture. Model credential input as write-only in OpenAPI and omit it from response types.

**Step 4: Regenerate and verify OpenAPI**

Run:

```bash
python scripts/generate_openapi.py
python -m pytest tests/test_openapi_contract.py tests/test_sdk_fixtures.py -v
```

Expected: PASS and checked-in OpenAPI matches the app contract.

**Step 5: Commit**

```bash
git add mixapi/api_contract.py openapi/openapi.json sdk-fixtures tests/test_openapi_contract.py tests/test_sdk_fixtures.py
git commit -m "docs: publish dynamic routing api contract"
```

### Task 17: Add the Idempotent SQLite Migration Command

**Files:**
- Create: `mixapi/migration/__init__.py`
- Create: `mixapi/migration/sqlite_import.py`
- Create: `mixapi/cli.py`
- Modify: `pyproject.toml`
- Test: `tests/integration/test_sqlite_migration.py`
- Create: `tests/fixtures/legacy_mixapi.sqlite3`

**Step 1: Write failing dry-run and apply tests**

Build a legacy SQLite fixture containing API keys, tenant policies, audits, usage, route decisions, settled spend, idempotency, circuits, and fractional timestamps. Cover:

- Dry run writes nothing.
- Apply preserves IDs and timestamps.
- Re-run is idempotent.
- Interrupted batches resume from the migration ledger.
- Counts, usage totals, and spend totals reconcile.
- Idempotency and circuits import into Redis.
- Existing hardcoded catalog plus configured environment endpoints become dynamic records.
- Missing real provider endpoints block active publication unless `--allow-disabled-providers` is supplied.
- Final report contains no secrets or key hashes.

**Step 2: Run focused tests**

Run: `python -m pytest tests/integration/test_sqlite_migration.py -v`

Expected: FAIL because the command does not exist.

**Step 3: Implement the CLI**

Add:

```toml
[project.scripts]
mixapi = "mixapi.cli:main"
```

Implement:

```text
mixapi migrate-sqlite --sqlite-path PATH --postgres-dsn DSN [--apply]
                       [--batch-size N] [--report PATH]
                       [--allow-disabled-providers]
```

Use read-only SQLite URI mode. Fingerprint the source file and store it in `sqlite_migration_runs`. Each destination insert uses stable natural/source identifiers and `ON CONFLICT` checks that reject divergent reruns.

**Step 4: Publish and verify the migrated snapshot**

On apply, publish the imported configuration and require a valid Redis active version unless disabled providers were explicitly allowed. Reconcile source/destination totals before marking the run complete.

**Step 5: Run focused tests**

Run: `python -m pytest tests/integration/test_sqlite_migration.py -v`

Expected: PASS.

**Step 6: Commit**

```bash
git add mixapi/migration mixapi/cli.py pyproject.toml tests/integration/test_sqlite_migration.py tests/fixtures/legacy_mixapi.sqlite3
git commit -m "feat: migrate sqlite installations to postgres redis"
```

### Task 18: Remove Legacy Runtime Paths and Complete Verification

**Files:**
- Delete: `mixapi/persistence.py`
- Delete: `mixapi/catalog.py`
- Remove: SQLite and in-memory runtime implementations from `mixapi/control_plane.py`, `mixapi/idempotency.py`, `mixapi/quota.py`, `mixapi/budget.py`, `mixapi/circuits.py`, `mixapi/route_decisions.py`, and `mixapi/usage.py`
- Modify: `mixapi/app.py`
- Modify: `scripts/generate_openapi.py`
- Create: `.github/workflows/ci.yml`
- Modify: `.gitignore`
- Modify: `TASKS.md`
- Create: `docs/operations/postgres-redis.md`
- Create: `tests/test_architecture_boundaries.py`
- Test: all tests

**Step 1: Add failing legacy-removal assertions**

Add a source-level test that rejects:

- `sqlite3` imports outside `mixapi/migration/`.
- Runtime references to `SQLiteDatabase`, `default_catalog`, `MIXAPI_OPENAI_BASE_URL`, `MIXAPI_GEMINI_BASE_URL`, `MIXAPI_ANTHROPIC_BASE_URL`, or `MIXAPI_OLLAMA_BASE_URL`.
- Application wiring of any `InMemory*` persistence or coordination service.
- `create_app(database_path=...)` or provider-specific base URL arguments.

Run: `python -m pytest tests/test_architecture_boundaries.py -v`

Expected: FAIL until legacy code is removed.

**Step 2: Delete legacy implementations and update tests**

Make `pytest` with real PostgreSQL/Redis the authoritative suite. Use an autouse fixture to truncate PostgreSQL tables, flush the configured Redis namespace, seed a test configuration, and publish an initial snapshot. Keep deterministic upstream behavior only through injected test adapters.

**Step 3: Add CI services and operations documentation**

CI must start PostgreSQL and Redis, wait for health, apply Alembic migrations, bootstrap test configuration, run tests, regenerate OpenAPI, and check diffs. Document environment variables, migration, bootstrap, readiness, backup, key rotation, snapshot rebuild, and Redis outage behavior.

**Step 4: Run the complete verification block**

Run:

```bash
docker compose up -d postgres redis
python scripts/wait_for_dependencies.py
python scripts/migrate.py upgrade head
python -m pytest -v
python scripts/generate_openapi.py
git diff --exit-code openapi/openapi.json
python -m compileall mixapi tests scripts
git diff --check
```

Expected: all tests pass, OpenAPI is unchanged after regeneration, compilation succeeds, and whitespace check is clean.

**Step 5: Verify the migration command manually**

Run:

```bash
mixapi migrate-sqlite \
  --sqlite-path tests/fixtures/legacy_mixapi.sqlite3 \
  --postgres-dsn "$MIXAPI_DATABASE_URL"
```

Expected: dry-run reconciliation report, zero writes, and no secret material.

Then run against an empty dedicated migration database with `--apply`, re-run it, and confirm the second run reports no divergent writes.

**Step 6: Update project tracking**

Mark PostgreSQL/Redis migration, dynamic provider connections, model mappings, snapshot publication, fail-closed routing, encryption, migration tooling, and contract coverage complete in `TASKS.md` only after the verification evidence exists.

**Step 7: Commit**

```bash
git add -A
git commit -m "refactor: require postgres redis dynamic control plane"
```

## Completion Criteria

The implementation is complete only when all of the following are true:

- Application startup requires PostgreSQL, Redis, and a valid master key.
- Alembic is the only runtime schema-management mechanism.
- PostgreSQL is the sole durable source of truth.
- Redis is the sole runtime coordination and active-snapshot source.
- Redis outage fails closed without PostgreSQL routing fallback or stale local routing.
- Provider connections are independently routable even when they share a protocol.
- Public aliases such as `gpt-5.5` can map across Gemini, Anthropic, and OpenAI-compatible vendors.
- Every request records and retains one configuration version.
- Configuration publication is transactional, versioned, validated, atomic, and recoverable.
- Provider credentials are AES-GCM encrypted and never exposed through APIs, logs, telemetry, reports, or Redis plaintext.
- Existing tenant authorization, usage, budgets, route history, fallback, structured output, streaming, and observability behavior remains covered.
- The SQLite migration is dry-run by default, resumable, idempotent, reconciled, and capable of publishing the initial snapshot.
- No runtime SQLite, hardcoded catalog, provider-specific environment base URL, or in-memory persistence path remains.
