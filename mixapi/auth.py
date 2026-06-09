from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from hmac import compare_digest

from fastapi import Header

from mixapi.errors import authentication_failed


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    project_id: str
    api_key_id: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class AdminPrincipal:
    actor_id: str


def configured_api_keys() -> dict[str, Principal]:
    keys = {
        "dev-key": Principal(
            tenant_id="tenant_dev",
            project_id="project_dev",
            api_key_id="key_dev",
            scopes=("models:read", "responses:create", "embeddings:create"),
        )
    }

    for index, raw_key in enumerate(_split_env_keys(os.getenv("MIXAPI_API_KEYS", "")), start=1):
        keys[raw_key] = Principal(
            tenant_id="tenant_env",
            project_id="project_env",
            api_key_id=f"key_env_{index}",
            scopes=("models:read", "responses:create", "embeddings:create"),
        )

    return keys


def authenticate(authorization: str | None = Header(default=None)) -> Principal:
    if not authorization:
        raise authentication_failed("missing_api_key", "Missing bearer API key.")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise authentication_failed("invalid_api_key", "Invalid bearer API key.")

    principal = configured_api_keys().get(token)
    if principal is None:
        raise authentication_failed("invalid_api_key", "Invalid bearer API key.")

    return principal


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


def _split_env_keys(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]
