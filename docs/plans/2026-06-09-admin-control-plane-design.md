# Admin Control Plane Design

## Scope

MixAPI will expose a small operator-only control plane for API keys and tenant runtime policy. The first release supports creating, listing, updating, and revoking service API keys; setting tenant model allowlists; assigning API-key spend budgets; and setting tenant routing defaults. It intentionally does not add provider credential management, projects, an admin UI, OIDC, or a general-purpose policy language.

Admin endpoints use a dedicated bearer credential configured through `MIXAPI_ADMIN_KEY` or the `admin_api_key` application factory argument. This credential is never accepted by public data-plane endpoints. Service keys are generated with high entropy, returned only from the create response, and persisted as a SHA-256 digest plus a short display prefix. Existing development and environment keys remain available as bootstrap credentials, but managed keys are resolved through the control-plane store.

## Architecture

A new control-plane module owns immutable records, request validation, key generation, secret hashing, and an in-memory store implementation. The SQLite persistence module provides the same store contract with tables for API keys, tenant policies, and append-only audit events. The application chooses the SQLite implementation when `database_path` is configured and otherwise uses the in-memory implementation.

Authentication becomes store-aware through an application dependency closure. Successful authentication resolves a managed key into the existing `Principal` shape and rejects revoked or expired records. The `/v1/models` endpoint filters logical models through both the key and tenant allowlists. Route planning receives the effective allowlist and tenant routing defaults, applies the allowlist as a hard eligibility filter, and uses the configured objective when a request does not provide one. Budget reservation asks the control-plane store for the API-key limit so different keys can have different budgets.

## API Surface

The initial endpoints are:

- `POST /admin/v1/api-keys`
- `GET /admin/v1/api-keys`
- `PATCH /admin/v1/api-keys/{api_key_id}`
- `DELETE /admin/v1/api-keys/{api_key_id}`
- `PUT /admin/v1/tenants/{tenant_id}/model-allowlist`
- `PUT /admin/v1/tenants/{tenant_id}/routing-policy`
- `GET /admin/v1/audit-events`

Key creation accepts `tenant_id`, `project_id`, optional `name`, `scopes`, `model_allowlist`, `budget_limit_usd`, and `expires_at`. Patch supports status, name, scopes, allowlist, budget, and expiry changes. A delete revokes rather than physically removes a key. Routing policy initially contains only `objective`; fallback behavior remains the data-plane default. Empty tenant or key allowlists mean unrestricted access.

All admin mutations append an audit record containing actor, action, target type and ID, tenant ID, timestamp, and a redacted before/after diff. Secret values and hashes never appear in audit output.

## Errors And Concurrency

Admin authentication failures use the existing normalized authentication envelope. Invalid payloads return `validation_error`; missing managed resources return `not_found`; duplicate identifiers return a conflict-style validation error. SQLite mutations use immediate transactions so key updates and audit writes commit atomically. The in-memory store uses a lock around mutations and snapshots.

Configuration is read directly from the selected store on each request. This is sufficient for the current modular monolith and guarantees immediate enforcement without introducing cache invalidation machinery before a multi-process control plane exists.

## Testing

Tests cover admin authentication isolation, one-time secret return, hashed persistence, listing redaction, key revocation and expiry, tenant and key allowlist enforcement, per-key budget enforcement, routing-default application and request override, audit-event redaction, SQLite restart persistence, invalid payloads, and tenant isolation. Existing public API tests remain unchanged through bootstrap key compatibility. The feature is complete only after focused tests, the full unit suite, compilation, and whitespace checks pass.
