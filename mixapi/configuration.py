from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Generic, TypeVar

from mixapi.models import SchemaSupport
from mixapi.secrets import EncryptedCredential


@dataclass(frozen=True)
class ProviderConnection:
    id: str
    name: str
    protocol: str
    base_url: str
    credential: EncryptedCredential
    timeout_seconds: Decimal
    status: str
    priority: int
    weight: int
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "timeout_seconds": str(self.timeout_seconds),
            "status": self.status,
            "priority": self.priority,
            "weight": self.weight,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            **self.credential.public_dict(),
        }


@dataclass(frozen=True)
class LogicalModelAlias:
    alias: str
    logical_model_id: str
    created_at: datetime | None = None


@dataclass(frozen=True)
class LogicalModel:
    id: str
    description: str
    status: str
    aliases: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "aliases": list(self.aliases),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
        }


@dataclass(frozen=True)
class ModelCandidate:
    id: str
    logical_model_id: str
    provider_connection_id: str
    upstream_model_id: str
    status: str
    priority: int
    weight: int
    context_window_tokens: int
    max_output_tokens: int
    input_modalities: tuple[str, ...]
    output_modalities: tuple[str, ...]
    tool_modes: tuple[str, ...]
    schema_support: SchemaSupport
    streaming_support: bool
    embeddings_support: bool
    retention_class: str
    regions: tuple[str, ...]
    pricing: dict[str, Any]
    native_features: tuple[str, ...]
    unsupported_parameters: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "logical_model_id": self.logical_model_id,
            "provider_connection_id": self.provider_connection_id,
            "upstream_model_id": self.upstream_model_id,
            "status": self.status,
            "priority": self.priority,
            "weight": self.weight,
            "context_window_tokens": self.context_window_tokens,
            "max_output_tokens": self.max_output_tokens,
            "input_modalities": list(self.input_modalities),
            "output_modalities": list(self.output_modalities),
            "tool_modes": list(self.tool_modes),
            "schema_support": self.schema_support,
            "streaming_support": self.streaming_support,
            "embeddings_support": self.embeddings_support,
            "retention_class": self.retention_class,
            "regions": list(self.regions),
            "pricing": self.pricing,
            "native_features": list(self.native_features),
            "unsupported_parameters": list(self.unsupported_parameters),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
        }


@dataclass(frozen=True)
class ConfigurationVersion:
    version: int
    status: str
    checksum: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    published_at: datetime | None
    failed_at: datetime | None


@dataclass(frozen=True)
class ConfigurationOutboxEvent:
    id: int
    configuration_version: int
    event_type: str
    payload: dict[str, Any]
    claimed_by: str | None
    claimed_at: datetime | None
    claim_expires_at: datetime | None
    processed_at: datetime | None
    attempts: int
    last_error: str | None
    created_at: datetime


T = TypeVar("T")


@dataclass(frozen=True)
class ConfigurationMutationResult(Generic[T]):
    resource: T
    version: ConfigurationVersion
    audit_id: str
    outbox_id: int


@dataclass(frozen=True)
class StoredConfiguration:
    providers: tuple[ProviderConnection, ...] = ()
    logical_models: tuple[LogicalModel, ...] = ()
    candidates: tuple[ModelCandidate, ...] = ()
    aliases: dict[str, str] = field(default_factory=dict)


# Descriptive aliases avoid ambiguity at call sites that also use routing models.
LogicalModelRecord = LogicalModel
ModelCandidateRecord = ModelCandidate
