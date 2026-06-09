from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from threading import Lock
from typing import Any, Protocol
from uuid import uuid4

from mixapi.errors import not_found


@dataclass(frozen=True)
class ApiKeyRecord:
    id: str
    tenant_id: str
    project_id: str
    name: str | None
    key_hash: str
    key_prefix: str
    scopes: tuple[str, ...]
    model_allowlist: tuple[str, ...]
    budget_limit_usd: Decimal | None
    expires_at: datetime | None
    status: str
    created_at: datetime
    updated_at: datetime

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "name": self.name,
            "key_prefix": self.key_prefix,
            "scopes": list(self.scopes),
            "model_allowlist": list(self.model_allowlist),
            "budget_limit_usd": (
                str(self.budget_limit_usd) if self.budget_limit_usd is not None else None
            ),
            "expires_at": _format_datetime(self.expires_at),
            "status": self.status,
            "created_at": _format_datetime(self.created_at),
            "updated_at": _format_datetime(self.updated_at),
        }


@dataclass(frozen=True)
class CreatedApiKey:
    record: ApiKeyRecord
    secret: str


@dataclass(frozen=True)
class TenantPolicy:
    tenant_id: str
    model_allowlist: tuple[str, ...] = ()
    routing_objective: str | None = None
    updated_at: datetime | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "model_allowlist": list(self.model_allowlist),
            "routing_objective": self.routing_objective,
            "updated_at": _format_datetime(self.updated_at),
        }


@dataclass(frozen=True)
class AuditEvent:
    id: str
    actor_id: str
    tenant_id: str
    action: str
    target_type: str
    target_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    created_at: datetime

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "actor_id": self.actor_id,
            "tenant_id": self.tenant_id,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "before": self.before,
            "after": self.after,
            "created_at": _format_datetime(self.created_at),
        }


class ControlPlaneStore(Protocol):
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
    ) -> CreatedApiKey: ...

    def resolve_api_key(self, secret: str) -> ApiKeyRecord | None: ...

    def get_api_key(self, api_key_id: str) -> ApiKeyRecord: ...

    def list_api_keys(self, tenant_id: str | None = None) -> list[ApiKeyRecord]: ...

    def update_api_key(
        self,
        api_key_id: str,
        changes: dict[str, Any],
        *,
        actor_id: str,
    ) -> ApiKeyRecord: ...

    def revoke_api_key(self, api_key_id: str, *, actor_id: str) -> ApiKeyRecord: ...

    def set_tenant_model_allowlist(
        self,
        tenant_id: str,
        model_allowlist: tuple[str, ...],
        *,
        actor_id: str,
    ) -> TenantPolicy: ...

    def set_tenant_routing_policy(
        self,
        tenant_id: str,
        objective: str | None,
        *,
        actor_id: str,
    ) -> TenantPolicy: ...

    def get_tenant_policy(self, tenant_id: str) -> TenantPolicy: ...

    def list_audit_events(self, tenant_id: str | None = None) -> list[AuditEvent]: ...


@dataclass
class InMemoryControlPlaneStore:
    _keys: dict[str, ApiKeyRecord] = field(default_factory=dict)
    _key_ids_by_hash: dict[str, str] = field(default_factory=dict)
    _tenant_policies: dict[str, TenantPolicy] = field(default_factory=dict)
    _audit_events: list[AuditEvent] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock)

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
        key_hash = hash_api_key(secret)
        now = datetime.now(timezone.utc)
        record = ApiKeyRecord(
            id=f"key_{uuid4().hex}",
            tenant_id=tenant_id,
            project_id=project_id,
            name=name,
            key_hash=key_hash,
            key_prefix=secret[:12],
            scopes=tuple(scopes),
            model_allowlist=tuple(model_allowlist),
            budget_limit_usd=budget_limit_usd,
            expires_at=expires_at,
            status="active",
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._keys[record.id] = record
            self._key_ids_by_hash[record.key_hash] = record.id
            self._append_audit(
                actor_id=actor_id,
                tenant_id=record.tenant_id,
                action="api_key.created",
                target_type="api_key",
                target_id=record.id,
                before=None,
                after=record.public_dict(),
                created_at=now,
            )
        return CreatedApiKey(record=record, secret=secret)

    def resolve_api_key(self, secret: str) -> ApiKeyRecord | None:
        key_hash = hash_api_key(secret)
        with self._lock:
            key_id = self._key_ids_by_hash.get(key_hash)
            record = self._keys.get(key_id) if key_id else None
        if record is None or record.status != "active":
            return None
        if record.expires_at is not None and record.expires_at <= datetime.now(timezone.utc):
            return None
        return record

    def get_api_key(self, api_key_id: str) -> ApiKeyRecord:
        with self._lock:
            record = self._keys.get(api_key_id)
        if record is None:
            raise not_found("api_key_not_found", "API key was not found.")
        return record

    def list_api_keys(self, tenant_id: str | None = None) -> list[ApiKeyRecord]:
        with self._lock:
            records = list(self._keys.values())
        if tenant_id is not None:
            records = [record for record in records if record.tenant_id == tenant_id]
        return sorted(records, key=lambda record: (record.created_at, record.id))

    def update_api_key(
        self,
        api_key_id: str,
        changes: dict[str, Any],
        *,
        actor_id: str,
    ) -> ApiKeyRecord:
        with self._lock:
            record = self._keys.get(api_key_id)
            if record is None:
                raise not_found("api_key_not_found", "API key was not found.")
            updated = replace(record, **changes, updated_at=datetime.now(timezone.utc))
            self._keys[api_key_id] = updated
            self._append_audit(
                actor_id=actor_id,
                tenant_id=updated.tenant_id,
                action="api_key.updated",
                target_type="api_key",
                target_id=updated.id,
                before=record.public_dict(),
                after=updated.public_dict(),
                created_at=updated.updated_at,
            )
            return updated

    def revoke_api_key(self, api_key_id: str, *, actor_id: str) -> ApiKeyRecord:
        with self._lock:
            record = self._keys.get(api_key_id)
            if record is None:
                raise not_found("api_key_not_found", "API key was not found.")
            now = datetime.now(timezone.utc)
            revoked = replace(record, status="revoked", updated_at=now)
            self._keys[api_key_id] = revoked
            self._append_audit(
                actor_id=actor_id,
                tenant_id=revoked.tenant_id,
                action="api_key.revoked",
                target_type="api_key",
                target_id=revoked.id,
                before=record.public_dict(),
                after=revoked.public_dict(),
                created_at=now,
            )
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
            model_allowlist=tuple(model_allowlist),
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
        with self._lock:
            return self._tenant_policies.get(tenant_id, TenantPolicy(tenant_id=tenant_id))

    def list_audit_events(self, tenant_id: str | None = None) -> list[AuditEvent]:
        with self._lock:
            events = list(self._audit_events)
        if tenant_id is not None:
            events = [event for event in events if event.tenant_id == tenant_id]
        return events

    def _update_tenant_policy(
        self,
        tenant_id: str,
        *,
        model_allowlist: tuple[str, ...] | None = None,
        routing_objective: str | None = None,
        actor_id: str,
        action: str,
    ) -> TenantPolicy:
        with self._lock:
            current = self._tenant_policies.get(tenant_id, TenantPolicy(tenant_id=tenant_id))
            now = datetime.now(timezone.utc)
            changes: dict[str, Any] = {"updated_at": now}
            if model_allowlist is not None:
                changes["model_allowlist"] = model_allowlist
            else:
                changes["routing_objective"] = routing_objective
            updated = replace(current, **changes)
            self._tenant_policies[tenant_id] = updated
            self._append_audit(
                actor_id=actor_id,
                tenant_id=tenant_id,
                action=action,
                target_type="tenant_policy",
                target_id=tenant_id,
                before=current.public_dict(),
                after=updated.public_dict(),
                created_at=now,
            )
            return updated

    def _append_audit(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        action: str,
        target_type: str,
        target_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        created_at: datetime,
    ) -> None:
        self._audit_events.append(
            AuditEvent(
                id=f"audit_{uuid4().hex}",
                actor_id=actor_id,
                tenant_id=tenant_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                before=before,
                after=after,
                created_at=created_at,
            )
        )


def generate_api_key() -> str:
    return f"mxapi_{secrets.token_urlsafe(32)}"


def hash_api_key(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
