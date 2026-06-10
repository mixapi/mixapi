# PostgreSQL and Redis Operations

MixAPI requires PostgreSQL for durable control-plane and accounting data and Redis for
active configuration snapshots, quotas, budgets, idempotency, circuits, and caches. The
service is not ready unless both dependencies and a published snapshot are available.

## Required Configuration

Set these environment variables for every API and worker process:

- `MIXAPI_DATABASE_URL`: PostgreSQL DSN.
- `MIXAPI_REDIS_URL`: Redis URL.
- `MIXAPI_MASTER_KEY`: base64-encoded 32-byte active encryption key.
- `MIXAPI_MASTER_KEY_VERSION`: positive integer identifying the active key.
- `MIXAPI_ADMIN_KEY`: bearer credential for `/admin/v1/*` routes.

Optional operational settings include `MIXAPI_PREVIOUS_MASTER_KEYS`,
`MIXAPI_REDIS_NAMESPACE`, PostgreSQL pool sizes, dependency timeouts, snapshot retention,
outbox lease intervals, and worker polling intervals. See `mixapi/settings.py` for names
and defaults.

## Initial Deployment

Start dependencies, wait for health, and apply schema migrations before starting MixAPI:

```bash
docker compose up -d postgres redis
python scripts/wait_for_dependencies.py
python scripts/migrate.py upgrade head
```

Create a JSON or YAML bootstrap document containing provider connections, logical models,
and candidates. Provider credentials are encrypted before PostgreSQL storage.

```bash
python scripts/bootstrap_configuration.py config/production.yaml --actor-id deploy
```

Bootstrap is idempotent for unchanged IDs and publishes every changed configuration
version to Redis. Do not put plaintext credentials in source control.

## Readiness

- `GET /health/live` confirms the process is running.
- `GET /health/ready` requires PostgreSQL, the expected Alembic revision, Redis, an active
  snapshot, and a master key capable of decrypting every active provider credential.

Load balancers should only send traffic when readiness returns HTTP 200. A missing or
invalid snapshot returns HTTP 503 on public `/v1/*` requests.

## Legacy SQLite Import

Run a dry run first, review the redacted report, then apply:

```bash
mixapi migrate-sqlite \
  --sqlite-path /var/lib/mixapi/mixapi.sqlite3 \
  --postgres-dsn "$MIXAPI_DATABASE_URL" \
  --report /var/tmp/mixapi-migration.json

mixapi migrate-sqlite \
  --sqlite-path /var/lib/mixapi/mixapi.sqlite3 \
  --postgres-dsn "$MIXAPI_DATABASE_URL" \
  --apply \
  --report /var/tmp/mixapi-migration.json
```

The command opens SQLite read-only, fingerprints the database and WAL, checkpoints batches
in PostgreSQL, restores Redis idempotency/circuit state, reconciles totals, and publishes a
snapshot. Configure all legacy provider endpoint and API-key environment variables before
apply. `--allow-disabled-providers` imports missing endpoints as disabled and permits a run
to complete without an active snapshot.

## Backup and Restore

PostgreSQL is authoritative. Take consistent `pg_dump` backups and retain the encryption
keys needed by every stored `credential_key_version`.

```bash
pg_dump --format=custom --file=mixapi.dump "$MIXAPI_DATABASE_URL"
pg_restore --clean --if-exists --dbname="$MIXAPI_DATABASE_URL" mixapi.dump
```

Redis persistence may shorten recovery but is not the authoritative configuration store.
After restoring PostgreSQL, start Redis and call `POST /admin/v1/configuration/rebuild` to
recreate and activate the latest published snapshot. Verify `/health/ready` before serving
traffic.

## Key Rotation

1. Add the old key to `MIXAPI_PREVIOUS_MASTER_KEYS` as `version:base64key`.
2. Deploy a new `MIXAPI_MASTER_KEY` and increment `MIXAPI_MASTER_KEY_VERSION`.
3. PATCH each provider with its credential to re-encrypt it under the active key.
4. Confirm readiness and that no provider row references the old key version.
5. Remove the old key from `MIXAPI_PREVIOUS_MASTER_KEYS` in a later deployment.

Never reuse a key version for different key material.

## Snapshot Recovery and Redis Outages

Use `POST /admin/v1/configuration/rebuild` when Redis was flushed or restored without the
active snapshot. The endpoint is lock protected and returns the active version.

Redis outages are fail closed: readiness becomes HTTP 503 and new public requests return
`configuration_unavailable` before authentication, accounting, or provider dispatch. A
request that already captured its immutable runtime context continues on that pinned
configuration version. After Redis recovers, rebuild the snapshot if the active pointer or
payload is missing, then verify readiness.

