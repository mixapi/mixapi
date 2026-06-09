from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from hmac import compare_digest

from fastapi import Header

from mixapi.control_plane import ApiKeyRecord, ControlPlaneStore, TenantPolicy
from mixapi.errors import authentication_failed, permission_denied


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    project_id: str
    api_key_id: str
    scopes: tuple[str, ...]
    model_allowlist: tuple[str, ...] | None = None
    budget_limit_usd: Decimal | None = None
    routing_objective: str | None = None


@dataclass(frozen=True)
class AdminPrincipal:
    actor_id: str


def configured_api_keys() -> dict[str, Principal]:
    keys = {
        "dev-key": Principal(
            tenant_id="tenant_dev",
            project_id="project_dev",
            api_key_id="key_dev",
            scopes=(
                "models:read",
                "responses:create",
                "embeddings:create",
                "usage:read",
                "route-decisions:read",
            ),
        )
    }

    for index, raw_key in enumerate(_split_env_keys(os.getenv("MIXAPI_API_KEYS", "")), start=1):
        keys[raw_key] = Principal(
            tenant_id="tenant_env",
            project_id="project_env",
            api_key_id=f"key_env_{index}",
            scopes=(
                "models:read",
                "responses:create",
                "embeddings:create",
                "usage:read",
                "route-decisions:read",
            ),
        )

    return keys


def authenticate(authorization: str | None = Header(default=None)) -> Principal:
    token = _bearer_token(authorization, "api_key", "API key")

    principal = configured_api_keys().get(token)
    if principal is None:
        raise authentication_failed("invalid_api_key", "Invalid bearer API key.")

    return principal


def build_service_authenticator(
    control_plane: ControlPlaneStore,
) -> Callable[..., Principal]:
    def authenticate_service(authorization: str | None = Header(default=None)) -> Principal:
        token = _bearer_token(authorization, "api_key", "API key")
        if principal := configured_api_keys().get(token):
            return principal

        record = control_plane.resolve_api_key(token)
        if record is None:
            raise authentication_failed("invalid_api_key", "Invalid bearer API key.")
        policy = control_plane.get_tenant_policy(record.tenant_id)
        return _managed_principal(record, policy)

    return authenticate_service


def require_scope(principal: Principal, required_scope: str) -> None:
    if required_scope not in principal.scopes:
        raise permission_denied(
            "missing_scope",
            f"API key requires the `{required_scope}` scope.",
        )


def build_admin_authenticator(expected_key: str | None) -> Callable[..., AdminPrincipal]:
    def authenticate_admin(authorization: str | None = Header(default=None)) -> AdminPrincipal:
        if not authorization:
            raise authentication_failed("missing_admin_key", "Missing admin bearer key.")

        scheme, _, token = authorization.partition(" ")
        if (
            scheme.lower() != "bearer"
            or not token
            or expected_key is None
            or not compare_digest(token, expected_key)
        ):
            raise authentication_failed("invalid_admin_key", "Invalid admin bearer key.")
        return AdminPrincipal(actor_id="admin")

    return authenticate_admin


def _managed_principal(record: ApiKeyRecord, policy: TenantPolicy) -> Principal:
    return Principal(
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        api_key_id=record.id,
        scopes=record.scopes,
        model_allowlist=_effective_model_allowlist(
            record.model_allowlist,
            policy.model_allowlist,
        ),
        budget_limit_usd=record.budget_limit_usd,
        routing_objective=policy.routing_objective,
    )


def _effective_model_allowlist(
    key_allowlist: tuple[str, ...],
    tenant_allowlist: tuple[str, ...],
) -> tuple[str, ...] | None:
    if not key_allowlist and not tenant_allowlist:
        return None
    if not key_allowlist:
        return tenant_allowlist
    if not tenant_allowlist:
        return key_allowlist
    tenant_models = set(tenant_allowlist)
    return tuple(model for model in key_allowlist if model in tenant_models)


def _bearer_token(
    authorization: str | None,
    error_suffix: str,
    label: str,
) -> str:
    if not authorization:
        raise authentication_failed(f"missing_{error_suffix}", f"Missing bearer {label}.")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise authentication_failed(f"invalid_{error_suffix}", f"Invalid bearer {label}.")
    return token


def _split_env_keys(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]
