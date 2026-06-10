from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol


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
                _format_decimal(self.budget_limit_usd)
                if self.budget_limit_usd is not None
                else None
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


def generate_api_key() -> str:
    return f"mxapi_{secrets.token_urlsafe(32)}"


def hash_api_key(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _format_decimal(value: Decimal) -> str:
    formatted = format(value, "f").rstrip("0").rstrip(".")
    whole, separator, fraction = formatted.partition(".")
    if not separator:
        return f"{whole}.00"
    if len(fraction) == 1:
        return f"{whole}.{fraction}0"
    return formatted
