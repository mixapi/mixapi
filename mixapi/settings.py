from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass, field


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    master_key: bytes
    master_key_version: int
    previous_master_keys: dict[int, bytes] = field(default_factory=dict)
    admin_api_key: str | None = None
    postgres_pool_min_size: int = 1
    postgres_pool_max_size: int = 10
    dependency_connect_timeout_seconds: float = 5.0
    redis_socket_timeout_seconds: float = 2.0
    auth_cache_ttl_seconds: int = 60
    redis_namespace: str = "mixapi"
    idempotency_ttl_seconds: int = 86_400
    snapshot_retention_count: int = 3
    snapshot_retention_ttl_seconds: int = 300
    configuration_publisher_interval_seconds: float = 0.5
    configuration_rebuild_interval_seconds: float = 60.0
    outbox_claim_lease_seconds: int = 30
    usage_intent_recovery_interval_seconds: float = 60.0
    usage_intent_stale_seconds: int = 300
    budget_reconciliation_interval_seconds: float = 1.0
    worker_shutdown_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.database_url.strip():
            raise ConfigurationError("MIXAPI_DATABASE_URL must be configured")
        if not self.redis_url.strip():
            raise ConfigurationError("MIXAPI_REDIS_URL must be configured")
        if len(self.master_key) != 32:
            raise ConfigurationError("MIXAPI_MASTER_KEY must decode to exactly 32 bytes")
        if self.master_key_version <= 0:
            raise ConfigurationError("MIXAPI_MASTER_KEY_VERSION must be a positive integer")
        if self.master_key_version in self.previous_master_keys:
            raise ConfigurationError("The active master key version cannot also be previous")
        if any(version <= 0 or len(key) != 32 for version, key in self.previous_master_keys.items()):
            raise ConfigurationError("MIXAPI_PREVIOUS_MASTER_KEYS contains an invalid key")
        if self.postgres_pool_min_size < 1:
            raise ConfigurationError("PostgreSQL pool minimum size must be positive")
        if self.postgres_pool_max_size < self.postgres_pool_min_size:
            raise ConfigurationError("PostgreSQL pool maximum size must be at least its minimum")
        if self.dependency_connect_timeout_seconds <= 0:
            raise ConfigurationError("Dependency connect timeout must be positive")
        if self.redis_socket_timeout_seconds <= 0:
            raise ConfigurationError("Redis socket timeout must be positive")
        if self.auth_cache_ttl_seconds <= 0:
            raise ConfigurationError("Auth cache TTL must be positive")
        if not self.redis_namespace or ":" in self.redis_namespace:
            raise ConfigurationError("Redis namespace must be non-empty and cannot contain ':'")
        if self.idempotency_ttl_seconds <= 0:
            raise ConfigurationError("Idempotency TTL must be positive")
        if self.snapshot_retention_count <= 0:
            raise ConfigurationError("Snapshot retention count must be positive")
        if self.snapshot_retention_ttl_seconds <= 0:
            raise ConfigurationError("Snapshot retention TTL must be positive")
        if self.configuration_publisher_interval_seconds <= 0:
            raise ConfigurationError("Configuration publisher interval must be positive")
        if self.configuration_rebuild_interval_seconds <= 0:
            raise ConfigurationError("Configuration rebuild interval must be positive")
        if self.outbox_claim_lease_seconds <= 0:
            raise ConfigurationError("Outbox claim lease must be positive")
        if self.usage_intent_recovery_interval_seconds <= 0:
            raise ConfigurationError("Usage intent recovery interval must be positive")
        if self.usage_intent_stale_seconds <= 0:
            raise ConfigurationError("Usage intent stale threshold must be positive")
        if self.budget_reconciliation_interval_seconds <= 0:
            raise ConfigurationError("Budget reconciliation interval must be positive")
        if self.worker_shutdown_timeout_seconds <= 0:
            raise ConfigurationError("Worker shutdown timeout must be positive")

    @classmethod
    def from_env(cls, *, database_url: str | None = None) -> Settings:
        database_url = database_url or _required_env("MIXAPI_DATABASE_URL")
        redis_url = _required_env("MIXAPI_REDIS_URL")
        master_key = _decode_key(_required_env("MIXAPI_MASTER_KEY"), "MIXAPI_MASTER_KEY")
        master_key_version = _positive_int(
            _required_env("MIXAPI_MASTER_KEY_VERSION"),
            "MIXAPI_MASTER_KEY_VERSION",
        )
        return cls(
            database_url=database_url,
            redis_url=redis_url,
            master_key=master_key,
            master_key_version=master_key_version,
            previous_master_keys=_parse_previous_keys(
                os.getenv("MIXAPI_PREVIOUS_MASTER_KEYS", "")
            ),
            admin_api_key=os.getenv("MIXAPI_ADMIN_KEY"),
            postgres_pool_min_size=_env_positive_int("MIXAPI_POSTGRES_POOL_MIN_SIZE", 1),
            postgres_pool_max_size=_env_positive_int("MIXAPI_POSTGRES_POOL_MAX_SIZE", 10),
            dependency_connect_timeout_seconds=_env_positive_float(
                "MIXAPI_DEPENDENCY_CONNECT_TIMEOUT_SECONDS",
                5.0,
            ),
            redis_socket_timeout_seconds=_env_positive_float(
                "MIXAPI_REDIS_SOCKET_TIMEOUT_SECONDS",
                2.0,
            ),
            auth_cache_ttl_seconds=_env_positive_int("MIXAPI_AUTH_CACHE_TTL_SECONDS", 60),
            redis_namespace=os.getenv("MIXAPI_REDIS_NAMESPACE", "mixapi").strip(),
            idempotency_ttl_seconds=_env_positive_int(
                "MIXAPI_IDEMPOTENCY_TTL_SECONDS",
                86_400,
            ),
            snapshot_retention_count=_env_positive_int(
                "MIXAPI_SNAPSHOT_RETENTION_COUNT",
                3,
            ),
            snapshot_retention_ttl_seconds=_env_positive_int(
                "MIXAPI_SNAPSHOT_RETENTION_TTL_SECONDS",
                300,
            ),
            configuration_publisher_interval_seconds=_env_positive_float(
                "MIXAPI_CONFIGURATION_PUBLISHER_INTERVAL_SECONDS",
                0.5,
            ),
            configuration_rebuild_interval_seconds=_env_positive_float(
                "MIXAPI_CONFIGURATION_REBUILD_INTERVAL_SECONDS",
                60.0,
            ),
            outbox_claim_lease_seconds=_env_positive_int(
                "MIXAPI_OUTBOX_CLAIM_LEASE_SECONDS",
                30,
            ),
            usage_intent_recovery_interval_seconds=_env_positive_float(
                "MIXAPI_USAGE_INTENT_RECOVERY_INTERVAL_SECONDS",
                60.0,
            ),
            usage_intent_stale_seconds=_env_positive_int(
                "MIXAPI_USAGE_INTENT_STALE_SECONDS",
                300,
            ),
            budget_reconciliation_interval_seconds=_env_positive_float(
                "MIXAPI_BUDGET_RECONCILIATION_INTERVAL_SECONDS",
                1.0,
            ),
            worker_shutdown_timeout_seconds=_env_positive_float(
                "MIXAPI_WORKER_SHUTDOWN_TIMEOUT_SECONDS",
                5.0,
            ),
        )


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ConfigurationError(f"{name} must be configured")
    return value.strip()


def _decode_key(value: str, name: str) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ConfigurationError(f"{name} must be valid base64") from error
    if len(decoded) != 32:
        raise ConfigurationError(f"{name} must decode to exactly 32 bytes")
    return decoded


def _positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a positive integer") from error
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be a positive integer")
    return parsed


def _env_positive_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None else _positive_int(value, name)


def _env_positive_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a positive number") from error
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be a positive number")
    return parsed


def _parse_previous_keys(value: str) -> dict[int, bytes]:
    if not value.strip():
        return {}
    keys: dict[int, bytes] = {}
    for item in value.split(","):
        raw_version, separator, raw_key = item.partition(":")
        if not separator:
            raise ConfigurationError(
                "MIXAPI_PREVIOUS_MASTER_KEYS entries must use version:base64key"
            )
        version = _positive_int(raw_version.strip(), "MIXAPI_PREVIOUS_MASTER_KEYS version")
        if version in keys:
            raise ConfigurationError("MIXAPI_PREVIOUS_MASTER_KEYS contains a duplicate version")
        keys[version] = _decode_key(raw_key.strip(), "MIXAPI_PREVIOUS_MASTER_KEYS key")
    return keys
