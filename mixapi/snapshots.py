from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from mixapi.configuration import (
    LogicalModel,
    ModelCandidate,
    ProviderConnection,
    StoredConfiguration,
)
from mixapi.secrets import EncryptedCredential


SNAPSHOT_SCHEMA_VERSION = 1


class SnapshotValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ConfigurationSnapshot:
    version: int
    generated_at: datetime
    providers: tuple[ProviderConnection, ...]
    logical_models: tuple[LogicalModel, ...]
    candidates: tuple[ModelCandidate, ...]
    aliases: dict[str, str]
    checksum: str
    schema_version: int = SNAPSHOT_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        version: int,
        generated_at: datetime,
        configuration: StoredConfiguration,
    ) -> ConfigurationSnapshot:
        if version <= 0:
            raise SnapshotValidationError("Snapshot version must be positive")
        if generated_at.tzinfo is None:
            raise SnapshotValidationError("Snapshot generated_at must include a timezone")
        snapshot = cls(
            version=version,
            generated_at=generated_at,
            providers=tuple(
                sorted(
                    (_normalize_provider(provider) for provider in configuration.providers),
                    key=lambda provider: provider.id,
                )
            ),
            logical_models=tuple(
                sorted(
                    (
                        replace(model, aliases=tuple(sorted(model.aliases)))
                        for model in configuration.logical_models
                    ),
                    key=lambda model: model.id,
                )
            ),
            candidates=tuple(
                sorted(
                    (_normalize_candidate(candidate) for candidate in configuration.candidates),
                    key=lambda candidate: candidate.id,
                )
            ),
            aliases=dict(sorted(configuration.aliases.items())),
            checksum="",
        )
        _validate_graph(snapshot)
        return replace(snapshot, checksum=_checksum(snapshot._content_dict()))

    @classmethod
    def from_json(cls, value: str | bytes) -> ConfigurationSnapshot:
        try:
            payload = json.loads(value)
            _require_object(payload, "snapshot")
            _require_keys(
                payload,
                {
                    "schema_version",
                    "version",
                    "generated_at",
                    "providers",
                    "logical_models",
                    "candidates",
                    "aliases",
                    "checksum",
                },
                "snapshot",
            )
            if payload["schema_version"] != SNAPSHOT_SCHEMA_VERSION:
                raise SnapshotValidationError(
                    f"Unsupported snapshot schema version: {payload['schema_version']}"
                )
            checksum = payload["checksum"]
            if not isinstance(checksum, str) or len(checksum) != 64:
                raise SnapshotValidationError("Snapshot checksum is invalid")
            content = {key: item for key, item in payload.items() if key != "checksum"}
            if not _constant_time_equal(checksum, _checksum(content)):
                raise SnapshotValidationError("Snapshot checksum does not match its payload")
            snapshot = cls(
                schema_version=payload["schema_version"],
                version=_require_int(payload["version"], "snapshot.version"),
                generated_at=_parse_datetime(payload["generated_at"], "snapshot.generated_at"),
                providers=tuple(
                    _provider_from_dict(item, index)
                    for index, item in enumerate(_require_list(payload["providers"], "providers"))
                ),
                logical_models=tuple(
                    _logical_model_from_dict(item, index)
                    for index, item in enumerate(
                        _require_list(payload["logical_models"], "logical_models")
                    )
                ),
                candidates=tuple(
                    _candidate_from_dict(item, index)
                    for index, item in enumerate(
                        _require_list(payload["candidates"], "candidates")
                    )
                ),
                aliases=_aliases_from_dict(payload["aliases"]),
                checksum=checksum,
            )
            if snapshot.version <= 0:
                raise SnapshotValidationError("Snapshot version must be positive")
            if tuple(sorted(snapshot.providers, key=lambda provider: provider.id)) != snapshot.providers:
                raise SnapshotValidationError("Snapshot providers are not in canonical order")
            if tuple(sorted(snapshot.logical_models, key=lambda model: model.id)) != snapshot.logical_models:
                raise SnapshotValidationError("Snapshot logical models are not in canonical order")
            if tuple(sorted(snapshot.candidates, key=lambda candidate: candidate.id)) != snapshot.candidates:
                raise SnapshotValidationError("Snapshot candidates are not in canonical order")
            if list(snapshot.aliases) != sorted(snapshot.aliases):
                raise SnapshotValidationError("Snapshot aliases are not in canonical order")
            _validate_graph(snapshot)
            return snapshot
        except SnapshotValidationError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise SnapshotValidationError("Snapshot payload is invalid") from error

    def to_json(self) -> str:
        payload = self._content_dict()
        expected = _checksum(payload)
        if not _constant_time_equal(self.checksum, expected):
            raise SnapshotValidationError("Snapshot checksum does not match its payload")
        payload["checksum"] = self.checksum
        return _canonical_json(payload)

    def resolve_model_id(self, requested_model: str) -> str | None:
        if any(model.id == requested_model for model in self.logical_models):
            return requested_model
        return self.aliases.get(requested_model)

    def _content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "version": self.version,
            "generated_at": self.generated_at.isoformat(),
            "providers": [_provider_to_dict(provider) for provider in self.providers],
            "logical_models": [_logical_model_to_dict(model) for model in self.logical_models],
            "candidates": [_candidate_to_dict(candidate) for candidate in self.candidates],
            "aliases": dict(self.aliases),
        }


def _provider_to_dict(provider: ProviderConnection) -> dict[str, Any]:
    return {
        "id": provider.id,
        "name": provider.name,
        "protocol": provider.protocol,
        "base_url": provider.base_url,
        "credential": {
            "key_version": provider.credential.key_version,
            "nonce": base64.b64encode(provider.credential.nonce).decode("ascii"),
            "ciphertext": base64.b64encode(provider.credential.ciphertext).decode("ascii"),
            "fingerprint": provider.credential.fingerprint,
        },
        "timeout_seconds": str(provider.timeout_seconds),
        "status": provider.status,
        "priority": provider.priority,
        "weight": provider.weight,
        "metadata": _json_value(provider.metadata),
        "created_at": provider.created_at.isoformat(),
        "updated_at": provider.updated_at.isoformat(),
        "deleted_at": provider.deleted_at.isoformat() if provider.deleted_at else None,
    }


def _provider_from_dict(value: Any, index: int) -> ProviderConnection:
    path = f"providers[{index}]"
    item = _require_object(value, path)
    _require_keys(
        item,
        {
            "id", "name", "protocol", "base_url", "credential", "timeout_seconds",
            "status", "priority", "weight", "metadata", "created_at", "updated_at",
            "deleted_at",
        },
        path,
    )
    credential = _require_object(item["credential"], f"{path}.credential")
    _require_keys(
        credential,
        {"key_version", "nonce", "ciphertext", "fingerprint"},
        f"{path}.credential",
    )
    return ProviderConnection(
        id=_require_str(item["id"], f"{path}.id"),
        name=_require_str(item["name"], f"{path}.name"),
        protocol=_require_str(item["protocol"], f"{path}.protocol"),
        base_url=_require_str(item["base_url"], f"{path}.base_url"),
        credential=EncryptedCredential(
            key_version=_require_int(credential["key_version"], f"{path}.credential.key_version"),
            nonce=_decode_base64(credential["nonce"], f"{path}.credential.nonce"),
            ciphertext=_decode_base64(
                credential["ciphertext"], f"{path}.credential.ciphertext"
            ),
            fingerprint=_require_str(
                credential["fingerprint"], f"{path}.credential.fingerprint"
            ),
        ),
        timeout_seconds=Decimal(
            _require_str(item["timeout_seconds"], f"{path}.timeout_seconds")
        ),
        status=_require_str(item["status"], f"{path}.status"),
        priority=_require_int(item["priority"], f"{path}.priority"),
        weight=_require_int(item["weight"], f"{path}.weight"),
        metadata=dict(_require_object(item["metadata"], f"{path}.metadata")),
        created_at=_parse_datetime(item["created_at"], f"{path}.created_at"),
        updated_at=_parse_datetime(item["updated_at"], f"{path}.updated_at"),
        deleted_at=_parse_optional_datetime(item["deleted_at"], f"{path}.deleted_at"),
    )


def _logical_model_to_dict(model: LogicalModel) -> dict[str, Any]:
    return {
        "id": model.id,
        "description": model.description,
        "status": model.status,
        "aliases": sorted(model.aliases),
        "created_at": model.created_at.isoformat(),
        "updated_at": model.updated_at.isoformat(),
        "deleted_at": model.deleted_at.isoformat() if model.deleted_at else None,
    }


def _logical_model_from_dict(value: Any, index: int) -> LogicalModel:
    path = f"logical_models[{index}]"
    item = _require_object(value, path)
    _require_keys(
        item,
        {"id", "description", "status", "aliases", "created_at", "updated_at", "deleted_at"},
        path,
    )
    aliases = tuple(
        _require_str(alias, f"{path}.aliases")
        for alias in _require_list(item["aliases"], f"{path}.aliases")
    )
    if aliases != tuple(sorted(aliases)):
        raise SnapshotValidationError(f"{path}.aliases are not in canonical order")
    return LogicalModel(
        id=_require_str(item["id"], f"{path}.id"),
        description=_require_str(item["description"], f"{path}.description"),
        status=_require_str(item["status"], f"{path}.status"),
        aliases=aliases,
        created_at=_parse_datetime(item["created_at"], f"{path}.created_at"),
        updated_at=_parse_datetime(item["updated_at"], f"{path}.updated_at"),
        deleted_at=_parse_optional_datetime(item["deleted_at"], f"{path}.deleted_at"),
    )


def _candidate_to_dict(candidate: ModelCandidate) -> dict[str, Any]:
    return {
        "id": candidate.id,
        "logical_model_id": candidate.logical_model_id,
        "provider_connection_id": candidate.provider_connection_id,
        "upstream_model_id": candidate.upstream_model_id,
        "status": candidate.status,
        "priority": candidate.priority,
        "weight": candidate.weight,
        "context_window_tokens": candidate.context_window_tokens,
        "max_output_tokens": candidate.max_output_tokens,
        "input_modalities": list(candidate.input_modalities),
        "output_modalities": list(candidate.output_modalities),
        "tool_modes": list(candidate.tool_modes),
        "schema_support": candidate.schema_support,
        "streaming_support": candidate.streaming_support,
        "embeddings_support": candidate.embeddings_support,
        "retention_class": candidate.retention_class,
        "regions": list(candidate.regions),
        "pricing": _json_value(candidate.pricing),
        "native_features": list(candidate.native_features),
        "unsupported_parameters": list(candidate.unsupported_parameters),
        "created_at": candidate.created_at.isoformat(),
        "updated_at": candidate.updated_at.isoformat(),
        "deleted_at": candidate.deleted_at.isoformat() if candidate.deleted_at else None,
    }


def _candidate_from_dict(value: Any, index: int) -> ModelCandidate:
    path = f"candidates[{index}]"
    item = _require_object(value, path)
    _require_keys(
        item,
        {
            "id", "logical_model_id", "provider_connection_id", "upstream_model_id",
            "status", "priority", "weight", "context_window_tokens", "max_output_tokens",
            "input_modalities", "output_modalities", "tool_modes", "schema_support",
            "streaming_support", "embeddings_support", "retention_class", "regions",
            "pricing", "native_features", "unsupported_parameters", "created_at",
            "updated_at", "deleted_at",
        },
        path,
    )
    return ModelCandidate(
        id=_require_str(item["id"], f"{path}.id"),
        logical_model_id=_require_str(item["logical_model_id"], f"{path}.logical_model_id"),
        provider_connection_id=_require_str(
            item["provider_connection_id"], f"{path}.provider_connection_id"
        ),
        upstream_model_id=_require_str(item["upstream_model_id"], f"{path}.upstream_model_id"),
        status=_require_str(item["status"], f"{path}.status"),
        priority=_require_int(item["priority"], f"{path}.priority"),
        weight=_require_int(item["weight"], f"{path}.weight"),
        context_window_tokens=_require_int(
            item["context_window_tokens"], f"{path}.context_window_tokens"
        ),
        max_output_tokens=_require_int(
            item["max_output_tokens"], f"{path}.max_output_tokens"
        ),
        input_modalities=_string_tuple(item["input_modalities"], f"{path}.input_modalities"),
        output_modalities=_string_tuple(item["output_modalities"], f"{path}.output_modalities"),
        tool_modes=_string_tuple(item["tool_modes"], f"{path}.tool_modes"),
        schema_support=_require_str(item["schema_support"], f"{path}.schema_support"),
        streaming_support=_require_bool(
            item["streaming_support"], f"{path}.streaming_support"
        ),
        embeddings_support=_require_bool(
            item["embeddings_support"], f"{path}.embeddings_support"
        ),
        retention_class=_require_str(item["retention_class"], f"{path}.retention_class"),
        regions=_string_tuple(item["regions"], f"{path}.regions"),
        pricing=dict(_require_object(item["pricing"], f"{path}.pricing")),
        native_features=_string_tuple(item["native_features"], f"{path}.native_features"),
        unsupported_parameters=_string_tuple(
            item["unsupported_parameters"], f"{path}.unsupported_parameters"
        ),
        created_at=_parse_datetime(item["created_at"], f"{path}.created_at"),
        updated_at=_parse_datetime(item["updated_at"], f"{path}.updated_at"),
        deleted_at=_parse_optional_datetime(item["deleted_at"], f"{path}.deleted_at"),
    )


def _normalize_provider(provider: ProviderConnection) -> ProviderConnection:
    return replace(provider, metadata=dict(_json_value(provider.metadata)))


def _normalize_candidate(candidate: ModelCandidate) -> ModelCandidate:
    return replace(candidate, pricing=dict(_json_value(candidate.pricing)))


def _validate_graph(snapshot: ConfigurationSnapshot) -> None:
    provider_ids = [provider.id for provider in snapshot.providers]
    model_ids = [model.id for model in snapshot.logical_models]
    candidate_ids = [candidate.id for candidate in snapshot.candidates]
    if len(provider_ids) != len(set(provider_ids)):
        raise SnapshotValidationError("Snapshot contains duplicate provider IDs")
    if len(model_ids) != len(set(model_ids)):
        raise SnapshotValidationError("Snapshot contains duplicate logical model IDs")
    if len(candidate_ids) != len(set(candidate_ids)):
        raise SnapshotValidationError("Snapshot contains duplicate candidate IDs")
    provider_id_set = set(provider_ids)
    model_id_set = set(model_ids)
    for alias, model_id in snapshot.aliases.items():
        if not isinstance(alias, str) or not isinstance(model_id, str):
            raise SnapshotValidationError("Snapshot aliases must map strings to strings")
        if model_id not in model_id_set:
            raise SnapshotValidationError(f"Alias {alias} references an unknown logical model")
    for candidate in snapshot.candidates:
        if candidate.provider_connection_id not in provider_id_set:
            raise SnapshotValidationError(
                f"Candidate {candidate.id} references an unknown provider connection"
            )
        if candidate.logical_model_id not in model_id_set:
            raise SnapshotValidationError(
                f"Candidate {candidate.id} references an unknown logical model"
            )


def _aliases_from_dict(value: Any) -> dict[str, str]:
    item = _require_object(value, "aliases")
    aliases: dict[str, str] = {}
    for alias, model_id in item.items():
        if not isinstance(alias, str):
            raise SnapshotValidationError("Alias names must be strings")
        aliases[alias] = _require_str(model_id, f"aliases.{alias}")
    return aliases


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SnapshotValidationError(f"Value is not JSON serializable: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _checksum(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _constant_time_equal(left: str, right: str) -> bool:
    return isinstance(left, str) and hmac.compare_digest(left, right)


def _require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapshotValidationError(f"{path} must be an object")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise SnapshotValidationError(f"{path} must be an array")
    return value


def _require_keys(value: dict[str, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        unknown = sorted(actual - expected)
        missing = sorted(expected - actual)
        details = []
        if unknown:
            details.append(f"unknown fields: {', '.join(unknown)}")
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        raise SnapshotValidationError(f"{path} has {'; '.join(details)}")


def _require_str(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise SnapshotValidationError(f"{path} must be a string")
    return value


def _require_int(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SnapshotValidationError(f"{path} must be an integer")
    return value


def _require_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise SnapshotValidationError(f"{path} must be a boolean")
    return value


def _parse_datetime(value: Any, path: str) -> datetime:
    parsed = datetime.fromisoformat(_require_str(value, path))
    if parsed.tzinfo is None:
        raise SnapshotValidationError(f"{path} must include a timezone")
    return parsed


def _parse_optional_datetime(value: Any, path: str) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value, path)


def _decode_base64(value: Any, path: str) -> bytes:
    try:
        return base64.b64decode(_require_str(value, path), validate=True)
    except ValueError as error:
        raise SnapshotValidationError(f"{path} must be valid base64") from error


def _string_tuple(value: Any, path: str) -> tuple[str, ...]:
    return tuple(
        _require_str(item, path) for item in _require_list(value, path)
    )
