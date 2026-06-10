from __future__ import annotations

import hashlib
import ipaddress
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mixapi.errors import MixAPIError, configuration_unavailable
from mixapi.postgres import PostgresPool
from mixapi.publication import ConfigurationPublisher
from mixapi.redis_runtime import RedisRuntime
from mixapi.repositories.configuration import (
    ConfigurationConflict,
    ConfigurationNotFound,
    PostgresConfigurationRepository,
)
from mixapi.runtime.snapshots import RedisSnapshotStore
from mixapi.secrets import CredentialAAD, CredentialCipher, InvalidCredentialCiphertext
from mixapi.snapshots import ConfigurationSnapshot


EXPECTED_SCHEMA_REVISION = "20260610_0001"


class BootstrapValidationError(ValueError):
    pass


class _SeedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderSeed(_SeedModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    protocol: Literal["openai-compatible", "anthropic", "gemini", "ollama"]
    base_url: str = Field(min_length=1, max_length=2048)
    credential: str
    timeout_seconds: Decimal = Decimal("30")
    status: Literal["active", "disabled"] = "active"
    priority: int = 100
    weight: int = Field(default=1, gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LogicalModelSeed(_SeedModel):
    id: str = Field(min_length=1, max_length=200)
    description: str = ""
    aliases: tuple[str, ...] = ()
    status: Literal["active", "disabled"] = "active"


class CandidateSeed(_SeedModel):
    id: str = Field(min_length=1, max_length=64)
    logical_model_id: str = Field(min_length=1, max_length=200)
    provider_connection_id: str = Field(min_length=1, max_length=64)
    upstream_model_id: str = Field(min_length=1, max_length=300)
    status: Literal["active", "disabled"] = "active"
    priority: int = 100
    weight: int = Field(default=1, gt=0)
    context_window_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(ge=0)
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    tool_modes: tuple[str, ...] = ()
    schema_support: Literal[
        "none", "json_mode", "best_effort_schema", "strict_json_schema"
    ] = "none"
    streaming_support: bool = False
    embeddings_support: bool = False
    retention_class: str = "standard"
    regions: tuple[str, ...] = ()
    pricing: dict[str, Any] = Field(default_factory=dict)
    native_features: tuple[str, ...] = ()
    unsupported_parameters: tuple[str, ...] = ()


class BootstrapDocument(_SeedModel):
    schema_version: Literal[1]
    providers: tuple[ProviderSeed, ...]
    logical_models: tuple[LogicalModelSeed, ...]
    candidates: tuple[CandidateSeed, ...]

    @model_validator(mode="after")
    def validate_graph(self) -> BootstrapDocument:
        provider_ids = [provider.id for provider in self.providers]
        model_ids = [model.id for model in self.logical_models]
        candidate_ids = [candidate.id for candidate in self.candidates]
        if not provider_ids or not model_ids or not candidate_ids:
            raise ValueError("seed requires providers, logical models, and candidates")
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("seed contains duplicate provider IDs")
        provider_names = [provider.name for provider in self.providers]
        if len(provider_names) != len(set(provider_names)):
            raise ValueError("seed contains duplicate provider names")
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("seed contains duplicate logical model IDs")
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("seed contains duplicate candidate IDs")
        aliases = [alias for model in self.logical_models for alias in model.aliases]
        if len(aliases) != len(set(aliases)) or set(aliases).intersection(model_ids):
            raise ValueError("seed contains duplicate or ambiguous aliases")
        provider_id_set = set(provider_ids)
        model_id_set = set(model_ids)
        candidates_by_model = {model_id: 0 for model_id in model_ids}
        for candidate in self.candidates:
            if candidate.provider_connection_id not in provider_id_set:
                raise ValueError(
                    f"candidate {candidate.id} references an unknown provider"
                )
            if candidate.logical_model_id not in model_id_set:
                raise ValueError(
                    f"candidate {candidate.id} references an unknown logical model"
                )
            if candidate.status == "active":
                provider = next(
                    item
                    for item in self.providers
                    if item.id == candidate.provider_connection_id
                )
                model = next(
                    item for item in self.logical_models if item.id == candidate.logical_model_id
                )
                if provider.status != "active" or model.status != "active":
                    raise ValueError(
                        "active candidates require active providers and logical models"
                    )
                candidates_by_model[candidate.logical_model_id] += 1
        if any(
            model.status == "active" and candidates_by_model[model.id] == 0
            for model in self.logical_models
        ):
            raise ValueError("every active logical model requires an active candidate")
        for provider in self.providers:
            if provider.timeout_seconds <= 0:
                raise ValueError("provider timeout must be positive")
            if not _is_safe_provider_url(provider.base_url):
                raise ValueError("provider base URL is invalid")
        mappings = [
            (
                candidate.logical_model_id,
                candidate.provider_connection_id,
                candidate.upstream_model_id,
            )
            for candidate in self.candidates
        ]
        if len(mappings) != len(set(mappings)):
            raise ValueError("seed contains duplicate candidate mappings")
        return self


@dataclass(frozen=True)
class BootstrapResult:
    changed: bool
    published_versions: tuple[int, ...]
    active_version: int


class BootstrapService:
    def __init__(
        self,
        repository: PostgresConfigurationRepository,
        publisher: ConfigurationPublisher,
        snapshots: RedisSnapshotStore,
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._snapshots = snapshots

    def apply(
        self,
        payload: dict[str, Any],
        *,
        actor_id: str = "bootstrap",
    ) -> BootstrapResult:
        try:
            document = BootstrapDocument.model_validate(payload)
        except ValidationError as error:
            raise BootstrapValidationError(_validation_message(error)) from error

        versions: list[int] = []
        try:
            for provider in document.providers:
                versions.extend(self._upsert_provider(provider, actor_id))
            for model in document.logical_models:
                version = self._upsert_logical_model(model, actor_id)
                if version is not None:
                    versions.append(version)
            for candidate in document.candidates:
                version = self._upsert_candidate(candidate, actor_id)
                if version is not None:
                    versions.append(version)
        except (ConfigurationConflict, ConfigurationNotFound, ValueError) as error:
            raise BootstrapValidationError("Seed conflicts with stored configuration") from error

        published: list[int] = []
        for version in sorted(set(versions)):
            self._publisher.publish(version)
            published.append(version)

        if not published:
            active_version = self._snapshots.active_version()
            if active_version is None:
                latest = self._repository.latest_configuration_version()
                if latest is None:
                    raise BootstrapValidationError("Seed did not produce a configuration version")
                if latest.status == "published":
                    self._publisher.rebuild()
                else:
                    self._publisher.publish(latest.version)
                active_version = self._snapshots.active_version()
        else:
            active_version = published[-1]
        if active_version is None:
            raise BootstrapValidationError("Seed publication did not activate a snapshot")
        return BootstrapResult(
            changed=bool(versions),
            published_versions=tuple(published),
            active_version=active_version,
        )

    def _upsert_provider(self, seed: ProviderSeed, actor_id: str) -> list[int]:
        versions: list[int] = []
        try:
            current = self._repository.get_provider(seed.id)
        except ConfigurationNotFound:
            mutation = self._repository.create_provider(
                provider_id=seed.id,
                name=seed.name,
                protocol=seed.protocol,
                base_url=seed.base_url,
                credential=seed.credential,
                timeout_seconds=seed.timeout_seconds,
                status=seed.status,
                priority=seed.priority,
                weight=seed.weight,
                metadata=seed.metadata,
                actor_id=actor_id,
            )
            return [mutation.version.version]

        changes = {
            key: value
            for key, value in {
                "name": seed.name,
                "protocol": seed.protocol,
                "base_url": seed.base_url,
                "timeout_seconds": seed.timeout_seconds,
                "status": seed.status,
                "priority": seed.priority,
                "weight": seed.weight,
                "metadata": seed.metadata,
            }.items()
            if getattr(current, key) != value
        }
        if changes:
            mutation = self._repository.update_provider(
                seed.id,
                actor_id=actor_id,
                **changes,
            )
            versions.append(mutation.version.version)
            current = mutation.resource
        if current.credential.fingerprint != _credential_fingerprint(seed.credential):
            mutation = self._repository.rotate_provider_credential(
                seed.id,
                seed.credential,
                actor_id=actor_id,
            )
            versions.append(mutation.version.version)
        return versions

    def _upsert_logical_model(
        self,
        seed: LogicalModelSeed,
        actor_id: str,
    ) -> int | None:
        aliases = tuple(sorted(seed.aliases))
        try:
            current = self._repository.get_logical_model(seed.id)
        except ConfigurationNotFound:
            return self._repository.create_logical_model(
                model_id=seed.id,
                description=seed.description,
                aliases=aliases,
                status=seed.status,
                actor_id=actor_id,
            ).version.version
        changes: dict[str, Any] = {}
        if current.description != seed.description:
            changes["description"] = seed.description
        if current.aliases != aliases:
            changes["aliases"] = aliases
        if current.status != seed.status:
            changes["status"] = seed.status
        if not changes:
            return None
        return self._repository.update_logical_model(
            seed.id,
            actor_id=actor_id,
            **changes,
        ).version.version

    def _upsert_candidate(self, seed: CandidateSeed, actor_id: str) -> int | None:
        values = seed.model_dump()
        candidate_id = values.pop("id")
        try:
            current = self._repository.get_candidate(candidate_id)
        except ConfigurationNotFound:
            return self._repository.create_candidate(
                candidate_id=candidate_id,
                actor_id=actor_id,
                **values,
            ).version.version
        changes = {
            key: value
            for key, value in values.items()
            if getattr(current, key) != value
        }
        if not changes:
            return None
        return self._repository.update_candidate(
            candidate_id,
            actor_id=actor_id,
            **changes,
        ).version.version


@dataclass(frozen=True)
class ReadinessStatus:
    ready: bool
    checks: dict[str, str]
    active_version: int | None


class ReadinessService:
    def __init__(
        self,
        pool: PostgresPool,
        redis_runtime: RedisRuntime,
        snapshots: RedisSnapshotStore,
        cipher: CredentialCipher,
    ) -> None:
        self._pool = pool
        self._redis = redis_runtime
        self._snapshots = snapshots
        self._cipher = cipher

    def check(self) -> ReadinessStatus:
        status, _snapshot = self._inspect()
        return status

    def require_snapshot(self) -> ConfigurationSnapshot:
        status, snapshot = self._inspect()
        if not status.ready or snapshot is None:
            raise configuration_unavailable()
        return snapshot

    def _inspect(self) -> tuple[ReadinessStatus, ConfigurationSnapshot | None]:
        checks: dict[str, str] = {}
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
            checks["postgres"] = "ok"
            checks["schema"] = (
                "ok"
                if row is not None and row["version_num"] == EXPECTED_SCHEMA_REVISION
                else "outdated"
            )
        except Exception:
            checks["postgres"] = "unavailable"
            checks["schema"] = "unknown"

        try:
            checks["redis"] = "ok" if self._redis.ping() else "unavailable"
        except Exception:
            checks["redis"] = "unavailable"

        snapshot: ConfigurationSnapshot | None = None
        active_version: int | None = None
        try:
            active_version = self._snapshots.active_version()
            if active_version is None:
                checks["snapshot"] = "missing"
            else:
                snapshot = self._snapshots.load(active_version)
                checks["snapshot"] = "ok"
        except MixAPIError:
            checks["snapshot"] = "invalid"

        if snapshot is None:
            checks["master_key"] = "unknown"
        else:
            try:
                for provider in snapshot.providers:
                    self._cipher.decrypt(
                        provider.credential,
                        CredentialAAD(
                            provider_id=provider.id,
                            protocol=provider.protocol,
                            field="api_key",
                        ),
                    )
                checks["master_key"] = "ok"
            except (InvalidCredentialCiphertext, ValueError):
                checks["master_key"] = "unavailable"

        ready = all(
            checks.get(name) == "ok"
            for name in ("postgres", "schema", "redis", "snapshot", "master_key")
        )
        return ReadinessStatus(ready, checks, active_version), snapshot


def load_bootstrap_document(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        raw = source.read_text(encoding="utf-8")
        if source.suffix.lower() == ".json":
            value = json.loads(raw)
        else:
            value = yaml.safe_load(raw)
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as error:
        raise BootstrapValidationError(f"Unable to read seed document: {source}") from error
    if not isinstance(value, dict):
        raise BootstrapValidationError("Seed document must contain an object")
    return value


def _credential_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _is_safe_provider_url(value: str) -> bool:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        return False
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return address.is_global


def _validation_message(error: ValidationError) -> str:
    first = error.errors(include_url=False)[0]
    location = ".".join(str(item) for item in first["loc"])
    return f"Invalid seed at {location or 'document'}: {first['msg']}"
