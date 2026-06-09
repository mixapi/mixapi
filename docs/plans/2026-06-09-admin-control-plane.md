# Admin Control Plane Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add operator-authenticated APIs that manage service keys, tenant model allowlists, per-key budgets, and tenant routing defaults with immediate data-plane enforcement and audit history.

**Architecture:** Define one control-plane store contract with in-memory and SQLite implementations. Build application-scoped authentication dependencies around that store, expose a narrow `/admin/v1` API, and pass effective key and tenant policy into model listing, routing, scope checks, and budget reservation.

**Tech Stack:** Python dataclasses and protocols, FastAPI dependencies and endpoints, SQLite transactions, `unittest`, `TestClient`.

### Task 1: Control-Plane Domain And In-Memory Store

**Files:**
- Create: `mixapi/control_plane.py`
- Create: `tests/test_control_plane.py`

**Step 1: Write failing domain tests**

Add tests that create a key, verify the returned secret resolves to a redacted record, revoke it, reject it afterward, update tenant model/routing policy, and list secret-free audit events.

**Step 2: Run tests to verify RED**

Run: `python3 -m unittest tests.test_control_plane -v`

Expected: import failure because `mixapi.control_plane` does not exist.

**Step 3: Implement the minimal domain API**

Add immutable `ApiKeyRecord`, `TenantPolicy`, and `AuditEvent` records; a `ControlPlaneStore` protocol; SHA-256 key hashing; URL-safe secret generation; validation helpers; and a lock-protected `InMemoryControlPlaneStore`. Empty allowlists represent unrestricted access. Store only hash and prefix, and redact both from public/audit dictionaries.

**Step 4: Run tests to verify GREEN**

Run: `python3 -m unittest tests.test_control_plane -v`

Expected: all domain tests pass.

**Step 5: Commit**

```bash
git add mixapi/control_plane.py tests/test_control_plane.py
git commit -m "feat: add control plane store"
```

### Task 2: SQLite Control-Plane Persistence

**Files:**
- Modify: `mixapi/persistence.py`
- Modify: `tests/test_persistence.py`

**Step 1: Write failing persistence tests**

Add tests that create a managed key and tenant policy through one application/store instance, recreate the store against the same database, authenticate the key record, observe the policy, and verify audit rows remain redacted. Add a concurrent mutation test proving updates and audit writes remain consistent.

**Step 2: Run tests to verify RED**

Run: `python3 -m unittest tests.test_persistence.PersistenceTest.test_control_plane_configuration_survives_application_recreation -v`

Expected: failure because the SQLite control-plane store and schema are absent.

**Step 3: Implement SQLite schema and store**

Add `api_keys`, `tenant_policies`, and `audit_events` tables and indexes. Implement `SQLiteControlPlaneStore` using immediate transactions for mutations and audit insertion. Serialize tuple and diff fields as canonical JSON and money as decimal strings.

**Step 4: Run focused persistence tests**

Run: `python3 -m unittest tests.test_persistence -v`

Expected: all persistence tests pass.

**Step 5: Commit**

```bash
git add mixapi/persistence.py tests/test_persistence.py
git commit -m "feat: persist control plane configuration"
```

### Task 3: Admin Authentication And HTTP APIs

**Files:**
- Modify: `mixapi/auth.py`
- Modify: `mixapi/app.py`
- Create: `tests/test_admin_api.py`

**Step 1: Write failing endpoint tests**

Cover missing/wrong admin credentials, separation from service credentials, key creation with one-time secret return, redacted listing, patch, revoke, tenant allowlist update, routing policy update, audit listing, invalid scopes/models/money/timestamps/objectives, and missing resources.

**Step 2: Run tests to verify RED**

Run: `python3 -m unittest tests.test_admin_api -v`

Expected: 404 responses because `/admin/v1` endpoints are absent.

**Step 3: Implement application dependencies and endpoints**

Add an `admin_api_key` factory argument and `MIXAPI_ADMIN_KEY` fallback. Build separate admin and service authentication dependencies. Instantiate the selected control-plane store, expose it as `app.state.control_plane`, and add the approved admin endpoints. Ensure mutation responses and audit events never include key material or hashes.

**Step 4: Run endpoint tests**

Run: `python3 -m unittest tests.test_admin_api -v`

Expected: all admin endpoint tests pass.

**Step 5: Commit**

```bash
git add mixapi/auth.py mixapi/app.py tests/test_admin_api.py
git commit -m "feat: expose admin control plane APIs"
```

### Task 4: Immediate Data-Plane Enforcement

**Files:**
- Modify: `mixapi/auth.py`
- Modify: `mixapi/budget.py`
- Modify: `mixapi/models.py`
- Modify: `mixapi/routing.py`
- Modify: `mixapi/app.py`
- Modify: `mixapi/persistence.py`
- Modify: `tests/test_admin_api.py`
- Modify: `tests/test_budgets.py`
- Modify: `tests/test_models_endpoint.py`
- Modify: `tests/test_request_validation.py`

**Step 1: Write failing integration tests**

Prove that managed keys authenticate; expiry/revocation denies access; endpoint scopes return permission errors; tenant and key model allowlists intersect for `/v1/models` and dispatch; a tenant routing objective is used only when the request omits one; request routing overrides the tenant default; and different API keys enforce different spend limits, including after SQLite restart.

**Step 2: Run tests to verify RED**

Run: `python3 -m unittest tests.test_admin_api tests.test_budgets tests.test_models_endpoint tests.test_request_validation -v`

Expected: failures showing managed configuration is not yet applied to public requests.

**Step 3: Implement minimal enforcement**

Resolve managed keys through the control-plane store into `Principal`. Add reusable scope checks. Compute the effective logical-model allowlist as the intersection of key and tenant policy. Filter model output and route candidates before scoring. Apply the tenant objective only when no request objective is supplied. Extend budget reservation with an optional per-request limit and enforce the lower of global and key limits.

**Step 4: Run focused tests**

Run: `python3 -m unittest tests.test_admin_api tests.test_budgets tests.test_models_endpoint tests.test_request_validation -v`

Expected: all focused tests pass.

**Step 5: Commit**

```bash
git add mixapi tests
git commit -m "feat: enforce managed tenant policy"
```

### Task 5: Verification And Backlog Completion

**Files:**
- Modify: `TASKS.md`

**Step 1: Run the full suite**

Run: `python3 -m unittest discover -s tests`

Expected: all tests pass without warnings.

**Step 2: Run static repository checks**

Run: `python3 -m compileall -q mixapi tests`

Run: `git diff --check`

Expected: both commands succeed with no output.

**Step 3: Review the implementation**

Check authentication separation, secret redaction, tenant isolation, transaction boundaries, normalized errors, dynamic enforcement, and regression risk. Correct issues through a failing test first.

**Step 4: Mark the task complete**

Update the admin API checkbox in `TASKS.md` only after all verification passes.

**Step 5: Commit**

```bash
git add TASKS.md
git commit -m "docs: complete admin API task"
```
