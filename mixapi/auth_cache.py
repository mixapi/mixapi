from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import redis

from mixapi.control_plane import ApiKeyRecord, TenantPolicy, hash_api_key
from mixapi.errors import control_plane_unavailable


@dataclass(frozen=True)
class CachedAuthentication:
    tenant_id: str
    project_id: str
    api_key_id: str
    scopes: tuple[str, ...]
    model_allowlist: tuple[str, ...] | None
    budget_limit_usd: Decimal | None
    routing_objective: str | None
    expires_at: datetime | None


class RedisAuthCache:
    def __init__(self, client: redis.Redis, *, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("Auth cache TTL must be positive")
        self._client = client
        self._ttl_seconds = ttl_seconds

    def resolve(
        self,
        secret: str,
        loader: Callable[[], tuple[ApiKeyRecord, TenantPolicy] | None],
    ) -> CachedAuthentication | None:
        key_hash = hash_api_key(secret)
        try:
            cached_value = self._client.get(self._cache_key(key_hash))
            if cached_value is not None:
                try:
                    cached = _decode_authentication(cached_value)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    self._client.delete(self._cache_key(key_hash))
                else:
                    if cached.expires_at is None or cached.expires_at > datetime.now(timezone.utc):
                        return cached
                    self._client.delete(self._cache_key(key_hash))
                    return None
            loaded = loader()
            if loaded is None:
                return None
            record, policy = loaded
            authentication = _authentication_from_records(record, policy)
            pipeline = self._client.pipeline(transaction=True)
            pipeline.setex(
                self._cache_key(key_hash),
                self._ttl_seconds,
                _encode_authentication(authentication),
            )
            tenant_key = self._tenant_key(record.tenant_id)
            pipeline.sadd(tenant_key, key_hash)
            pipeline.expire(tenant_key, self._ttl_seconds)
            pipeline.execute()
            return authentication
        except (redis.RedisError, OSError) as error:
            raise control_plane_unavailable() from error

    def delete_key_hash(self, key_hash: str) -> None:
        try:
            self._client.delete(self._cache_key(key_hash))
        except (redis.RedisError, OSError) as error:
            raise control_plane_unavailable() from error

    def delete_tenant(self, tenant_id: str) -> None:
        tenant_key = self._tenant_key(tenant_id)
        try:
            key_hashes = self._client.smembers(tenant_key)
            pipeline = self._client.pipeline(transaction=True)
            if key_hashes:
                pipeline.delete(*(self._cache_key(value) for value in key_hashes))
            pipeline.delete(tenant_key)
            pipeline.execute()
        except (redis.RedisError, OSError) as error:
            raise control_plane_unavailable() from error

    @staticmethod
    def _cache_key(key_hash: str) -> str:
        return f"mixapi:auth:v1:key:{key_hash}"

    @staticmethod
    def _tenant_key(tenant_id: str) -> str:
        return f"mixapi:auth:v1:tenant:{tenant_id}"


def _authentication_from_records(
    record: ApiKeyRecord,
    policy: TenantPolicy,
) -> CachedAuthentication:
    return CachedAuthentication(
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
        expires_at=record.expires_at,
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


def _encode_authentication(authentication: CachedAuthentication) -> str:
    return json.dumps(
        {
            "version": 1,
            "tenant_id": authentication.tenant_id,
            "project_id": authentication.project_id,
            "api_key_id": authentication.api_key_id,
            "scopes": list(authentication.scopes),
            "model_allowlist": (
                list(authentication.model_allowlist)
                if authentication.model_allowlist is not None
                else None
            ),
            "budget_limit_usd": (
                str(authentication.budget_limit_usd)
                if authentication.budget_limit_usd is not None
                else None
            ),
            "routing_objective": authentication.routing_objective,
            "expires_at": (
                authentication.expires_at.isoformat()
                if authentication.expires_at is not None
                else None
            ),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_authentication(value: str | bytes) -> CachedAuthentication:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    payload: dict[str, Any] = json.loads(value)
    if payload["version"] != 1:
        raise ValueError("Unsupported auth cache version")
    raw_allowlist = payload["model_allowlist"]
    return CachedAuthentication(
        tenant_id=payload["tenant_id"],
        project_id=payload["project_id"],
        api_key_id=payload["api_key_id"],
        scopes=tuple(payload["scopes"]),
        model_allowlist=tuple(raw_allowlist) if raw_allowlist is not None else None,
        budget_limit_usd=(
            Decimal(payload["budget_limit_usd"])
            if payload["budget_limit_usd"] is not None
            else None
        ),
        routing_objective=payload["routing_objective"],
        expires_at=(
            datetime.fromisoformat(payload["expires_at"])
            if payload["expires_at"] is not None
            else None
        ),
    )
