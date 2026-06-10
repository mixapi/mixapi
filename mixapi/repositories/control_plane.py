from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from mixapi.auth_cache import RedisAuthCache
from mixapi.control_plane import (
    ApiKeyRecord,
    AuditEvent,
    CreatedApiKey,
    TenantPolicy,
    generate_api_key,
    hash_api_key,
)
from mixapi.errors import not_found
from mixapi.postgres import PostgresPool


class PostgresControlPlaneStore:
    def __init__(
        self,
        pool: PostgresPool,
        *,
        auth_cache: RedisAuthCache | None = None,
    ) -> None:
        self._pool = pool
        self._auth_cache = auth_cache

    def create_api_key(
        self,
        *,
        tenant_id: str,
        project_id: str,
        name: str | None,
        scopes: tuple[str, ...],
        model_allowlist: tuple[str, ...],
        budget_limit_usd: Decimal | None,
        expires_at: datetime | None,
        actor_id: str,
    ) -> CreatedApiKey:
        secret = generate_api_key()
        record_id = f"key_{uuid.uuid4().hex}"
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO api_keys (
                    id, tenant_id, project_id, name, key_hash, key_prefix,
                    scopes, model_allowlist, budget_limit_usd, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    record_id,
                    tenant_id,
                    project_id,
                    name,
                    hash_api_key(secret),
                    secret[:12],
                    Jsonb(list(scopes)),
                    Jsonb(list(model_allowlist)),
                    budget_limit_usd,
                    expires_at,
                ),
            ).fetchone()
            record = _api_key_from_row(row)
            self._insert_audit(
                connection,
                actor_id=actor_id,
                tenant_id=tenant_id,
                action="api_key.created",
                target_type="api_key",
                target_id=record.id,
                before=None,
                after=record.public_dict(),
            )
        return CreatedApiKey(record=record, secret=secret)

    def resolve_api_key(self, secret: str) -> ApiKeyRecord | None:
        key_hash = hash_api_key(secret)
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM api_keys
                WHERE key_hash = %s AND status = 'active'
                  AND (expires_at IS NULL OR expires_at > now())
                """,
                (key_hash,),
            ).fetchone()
        return _api_key_from_row(row) if row is not None else None

    def get_api_key(self, api_key_id: str) -> ApiKeyRecord:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM api_keys WHERE id = %s",
                (api_key_id,),
            ).fetchone()
        if row is None:
            raise not_found("api_key_not_found", "API key was not found.")
        return _api_key_from_row(row)

    def list_api_keys(self, tenant_id: str | None = None) -> list[ApiKeyRecord]:
        with self._pool.connection() as connection:
            if tenant_id is None:
                rows = connection.execute(
                    "SELECT * FROM api_keys ORDER BY created_at, id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM api_keys
                    WHERE tenant_id = %s ORDER BY created_at, id
                    """,
                    (tenant_id,),
                ).fetchall()
        return [_api_key_from_row(row) for row in rows]

    def update_api_key(
        self,
        api_key_id: str,
        changes: dict[str, Any],
        *,
        actor_id: str,
    ) -> ApiKeyRecord:
        allowed = {
            "name",
            "scopes",
            "model_allowlist",
            "budget_limit_usd",
            "expires_at",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported API key fields: {', '.join(sorted(unknown))}")
        with self._pool.connection() as connection:
            current_row = connection.execute(
                "SELECT * FROM api_keys WHERE id = %s FOR UPDATE",
                (api_key_id,),
            ).fetchone()
            if current_row is None:
                raise not_found("api_key_not_found", "API key was not found.")
            current = _api_key_from_row(current_row)
            values = {
                "name": current.name,
                "scopes": current.scopes,
                "model_allowlist": current.model_allowlist,
                "budget_limit_usd": current.budget_limit_usd,
                "expires_at": current.expires_at,
                **changes,
            }
            row = connection.execute(
                """
                UPDATE api_keys SET
                    name = %s, scopes = %s, model_allowlist = %s,
                    budget_limit_usd = %s, expires_at = %s, updated_at = now()
                WHERE id = %s RETURNING *
                """,
                (
                    values["name"],
                    Jsonb(list(values["scopes"])),
                    Jsonb(list(values["model_allowlist"])),
                    values["budget_limit_usd"],
                    values["expires_at"],
                    api_key_id,
                ),
            ).fetchone()
            updated = _api_key_from_row(row)
            self._insert_audit(
                connection,
                actor_id=actor_id,
                tenant_id=updated.tenant_id,
                action="api_key.updated",
                target_type="api_key",
                target_id=updated.id,
                before=current.public_dict(),
                after=updated.public_dict(),
            )
            self._invalidate_key(current.key_hash)
            return updated

    def revoke_api_key(self, api_key_id: str, *, actor_id: str) -> ApiKeyRecord:
        with self._pool.connection() as connection:
            current_row = connection.execute(
                "SELECT * FROM api_keys WHERE id = %s FOR UPDATE",
                (api_key_id,),
            ).fetchone()
            if current_row is None:
                raise not_found("api_key_not_found", "API key was not found.")
            current = _api_key_from_row(current_row)
            row = connection.execute(
                """
                UPDATE api_keys
                SET status = 'revoked', updated_at = now()
                WHERE id = %s RETURNING *
                """,
                (api_key_id,),
            ).fetchone()
            revoked = _api_key_from_row(row)
            self._insert_audit(
                connection,
                actor_id=actor_id,
                tenant_id=revoked.tenant_id,
                action="api_key.revoked",
                target_type="api_key",
                target_id=revoked.id,
                before=current.public_dict(),
                after=revoked.public_dict(),
            )
            self._invalidate_key(current.key_hash)
            return revoked

    def set_tenant_model_allowlist(
        self,
        tenant_id: str,
        model_allowlist: tuple[str, ...],
        *,
        actor_id: str,
    ) -> TenantPolicy:
        return self._update_tenant_policy(
            tenant_id,
            model_allowlist=model_allowlist,
            actor_id=actor_id,
            action="tenant.model_allowlist.updated",
        )

    def set_tenant_routing_policy(
        self,
        tenant_id: str,
        objective: str | None,
        *,
        actor_id: str,
    ) -> TenantPolicy:
        return self._update_tenant_policy(
            tenant_id,
            routing_objective=objective,
            actor_id=actor_id,
            action="tenant.routing_policy.updated",
        )

    def get_tenant_policy(self, tenant_id: str) -> TenantPolicy:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM tenant_policies WHERE tenant_id = %s",
                (tenant_id,),
            ).fetchone()
        if row is None:
            return TenantPolicy(tenant_id=tenant_id)
        return _tenant_policy_from_row(row)

    def list_audit_events(self, tenant_id: str | None = None) -> list[AuditEvent]:
        with self._pool.connection() as connection:
            if tenant_id is None:
                rows = connection.execute(
                    "SELECT * FROM audit_events ORDER BY sequence_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM audit_events
                    WHERE tenant_id = %s ORDER BY sequence_id
                    """,
                    (tenant_id,),
                ).fetchall()
        return [_audit_from_row(row) for row in rows]

    def _update_tenant_policy(
        self,
        tenant_id: str,
        *,
        actor_id: str,
        action: str,
        model_allowlist: tuple[str, ...] | None = None,
        routing_objective: str | None = None,
    ) -> TenantPolicy:
        with self._pool.connection() as connection:
            current_row = connection.execute(
                "SELECT * FROM tenant_policies WHERE tenant_id = %s FOR UPDATE",
                (tenant_id,),
            ).fetchone()
            current = (
                _tenant_policy_from_row(current_row)
                if current_row is not None
                else TenantPolicy(tenant_id=tenant_id)
            )
            next_allowlist = (
                current.model_allowlist if model_allowlist is None else model_allowlist
            )
            next_objective = (
                current.routing_objective
                if model_allowlist is not None
                else routing_objective
            )
            row = connection.execute(
                """
                INSERT INTO tenant_policies (
                    tenant_id, model_allowlist, routing_objective
                ) VALUES (%s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    model_allowlist = EXCLUDED.model_allowlist,
                    routing_objective = EXCLUDED.routing_objective,
                    updated_at = now()
                RETURNING *
                """,
                (tenant_id, Jsonb(list(next_allowlist)), next_objective),
            ).fetchone()
            updated = _tenant_policy_from_row(row)
            self._insert_audit(
                connection,
                actor_id=actor_id,
                tenant_id=tenant_id,
                action=action,
                target_type="tenant_policy",
                target_id=tenant_id,
                before=current.public_dict(),
                after=updated.public_dict(),
            )
            self._invalidate_tenant(tenant_id)
            return updated

    def _invalidate_key(self, key_hash: str) -> None:
        if self._auth_cache is not None:
            self._auth_cache.delete_key_hash(key_hash)

    def _invalidate_tenant(self, tenant_id: str) -> None:
        if self._auth_cache is not None:
            self._auth_cache.delete_tenant(tenant_id)

    @staticmethod
    def _insert_audit(
        connection: Connection,
        *,
        actor_id: str,
        tenant_id: str,
        action: str,
        target_type: str,
        target_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                id, actor_id, tenant_id, action, target_type, target_id, before, after
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                f"audit_{uuid.uuid4().hex}",
                actor_id,
                tenant_id,
                action,
                target_type,
                target_id,
                Jsonb(before) if before is not None else None,
                Jsonb(after) if after is not None else None,
            ),
        )


def _api_key_from_row(row: dict[str, Any]) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        project_id=row["project_id"],
        name=row["name"],
        key_hash=row["key_hash"],
        key_prefix=row["key_prefix"],
        scopes=tuple(row["scopes"]),
        model_allowlist=tuple(row["model_allowlist"]),
        budget_limit_usd=row["budget_limit_usd"],
        expires_at=row["expires_at"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _tenant_policy_from_row(row: dict[str, Any]) -> TenantPolicy:
    return TenantPolicy(
        tenant_id=row["tenant_id"],
        model_allowlist=tuple(row["model_allowlist"]),
        routing_objective=row["routing_objective"],
        updated_at=row["updated_at"],
    )


def _audit_from_row(row: dict[str, Any]) -> AuditEvent:
    return AuditEvent(
        id=row["id"],
        actor_id=row["actor_id"],
        tenant_id=row["tenant_id"],
        action=row["action"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        before=row["before"],
        after=row["after"],
        created_at=row["created_at"],
    )
