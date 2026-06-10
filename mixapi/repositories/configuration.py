from __future__ import annotations

import ipaddress
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable
from urllib.parse import urlsplit

from psycopg import Connection
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from mixapi.configuration import (
    ConfigurationMutationResult,
    ConfigurationOutboxEvent,
    ConfigurationVersion,
    LogicalModelRecord,
    ModelCandidateRecord,
    ProviderConnection,
    StoredConfiguration,
)
from mixapi.postgres import PostgresPool
from mixapi.secrets import CredentialAAD, CredentialCipher, EncryptedCredential


class ConfigurationConflict(ValueError):
    pass


class ConfigurationNotFound(LookupError):
    pass


_UNSET = object()
_PROVIDER_PROTOCOLS = {"openai-compatible", "anthropic", "gemini", "ollama"}
_SCHEMA_SUPPORT = {"none", "json_mode", "best_effort_schema", "strict_json_schema"}


class PostgresConfigurationRepository:
    def __init__(
        self,
        pool: PostgresPool,
        cipher: CredentialCipher,
        *,
        allow_private_upstreams: bool = False,
    ) -> None:
        self._pool = pool
        self._cipher = cipher
        self._allow_private_upstreams = allow_private_upstreams

    def create_provider(
        self,
        *,
        provider_id: str | None = None,
        name: str,
        protocol: str,
        base_url: str,
        credential: str,
        actor_id: str,
        timeout_seconds: Decimal = Decimal("30"),
        status: str = "active",
        priority: int = 100,
        weight: int = 1,
        metadata: dict[str, Any] | None = None,
    ) -> ConfigurationMutationResult[ProviderConnection]:
        provider_id = provider_id or f"provider_{uuid.uuid4().hex}"
        _validate_provider(protocol, base_url, timeout_seconds, status, weight, self._allow_private_upstreams)
        envelope = self._cipher.encrypt(
            credential,
            CredentialAAD(provider_id=provider_id, protocol=protocol, field="api_key"),
        )
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    INSERT INTO provider_connections (
                        id, name, protocol, base_url, credential_ciphertext,
                        credential_nonce, credential_key_version,
                        credential_fingerprint, timeout_seconds, status,
                        priority, weight, metadata
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    ) RETURNING *
                    """,
                    (
                        provider_id,
                        name,
                        protocol,
                        base_url,
                        envelope.ciphertext,
                        envelope.nonce,
                        envelope.key_version,
                        envelope.fingerprint,
                        timeout_seconds,
                        status,
                        priority,
                        weight,
                        Jsonb(metadata or {}),
                    ),
                ).fetchone()
                resource = _provider_from_row(row)
                return self._record_mutation(
                    connection,
                    resource=resource,
                    actor_id=actor_id,
                    action="provider.created",
                    target_type="provider_connection",
                    before=None,
                    after=resource.public_dict(),
                )
        except UniqueViolation as error:
            raise ConfigurationConflict("provider name already exists") from error

    def get_provider(self, provider_id: str, *, include_deleted: bool = False) -> ProviderConnection:
        clause = "" if include_deleted else " AND deleted_at IS NULL"
        with self._pool.connection() as connection:
            row = connection.execute(
                f"SELECT * FROM provider_connections WHERE id = %s{clause}",
                (provider_id,),
            ).fetchone()
        if row is None:
            raise ConfigurationNotFound(f"provider not found: {provider_id}")
        return _provider_from_row(row)

    def list_providers(self, *, include_deleted: bool = False) -> list[ProviderConnection]:
        clause = "" if include_deleted else "WHERE deleted_at IS NULL"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM provider_connections {clause} ORDER BY name, id"
            ).fetchall()
        return [_provider_from_row(row) for row in rows]

    def update_provider(
        self,
        provider_id: str,
        *,
        actor_id: str,
        name: str | object = _UNSET,
        protocol: str | object = _UNSET,
        base_url: str | object = _UNSET,
        timeout_seconds: Decimal | object = _UNSET,
        status: str | object = _UNSET,
        priority: int | object = _UNSET,
        weight: int | object = _UNSET,
        metadata: dict[str, Any] | object = _UNSET,
        expected_updated_at: datetime | None = None,
    ) -> ConfigurationMutationResult[ProviderConnection]:
        try:
            with self._pool.connection() as connection:
                current_row = connection.execute(
                    "SELECT * FROM provider_connections WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                    (provider_id,),
                ).fetchone()
                if current_row is None:
                    raise ConfigurationNotFound(f"provider not found: {provider_id}")
                current = _provider_from_row(current_row)
                if expected_updated_at is not None and current.updated_at != expected_updated_at:
                    raise ConfigurationConflict("provider was modified by another request")
                next_protocol = current.protocol if protocol is _UNSET else str(protocol)
                next_url = current.base_url if base_url is _UNSET else str(base_url)
                next_timeout = current.timeout_seconds if timeout_seconds is _UNSET else Decimal(timeout_seconds)
                next_status = current.status if status is _UNSET else str(status)
                next_weight = current.weight if weight is _UNSET else int(weight)
                _validate_provider(
                    next_protocol,
                    next_url,
                    next_timeout,
                    next_status,
                    next_weight,
                    self._allow_private_upstreams,
                )
                envelope = current.credential
                if next_protocol != current.protocol:
                    plaintext = self.decrypt_provider_credential(current)
                    envelope = self._cipher.encrypt(
                        plaintext,
                        CredentialAAD(provider_id=provider_id, protocol=next_protocol, field="api_key"),
                    )
                row = connection.execute(
                    """
                    UPDATE provider_connections SET
                        name = %s, protocol = %s, base_url = %s,
                        credential_ciphertext = %s, credential_nonce = %s,
                        credential_key_version = %s, credential_fingerprint = %s,
                        timeout_seconds = %s, status = %s, priority = %s,
                        weight = %s, metadata = %s, updated_at = now()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (
                        current.name if name is _UNSET else str(name),
                        next_protocol,
                        next_url,
                        envelope.ciphertext,
                        envelope.nonce,
                        envelope.key_version,
                        envelope.fingerprint,
                        next_timeout,
                        next_status,
                        current.priority if priority is _UNSET else int(priority),
                        next_weight,
                        Jsonb(current.metadata if metadata is _UNSET else metadata),
                        provider_id,
                    ),
                ).fetchone()
                resource = _provider_from_row(row)
                return self._record_mutation(
                    connection,
                    resource=resource,
                    actor_id=actor_id,
                    action="provider.updated",
                    target_type="provider_connection",
                    before=current.public_dict(),
                    after=resource.public_dict(),
                )
        except UniqueViolation as error:
            raise ConfigurationConflict("provider name already exists") from error

    def rotate_provider_credential(
        self,
        provider_id: str,
        credential: str,
        *,
        actor_id: str,
        expected_updated_at: datetime | None = None,
    ) -> ConfigurationMutationResult[ProviderConnection]:
        with self._pool.connection() as connection:
            current_row = connection.execute(
                "SELECT * FROM provider_connections WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                (provider_id,),
            ).fetchone()
            if current_row is None:
                raise ConfigurationNotFound(f"provider not found: {provider_id}")
            current = _provider_from_row(current_row)
            if expected_updated_at is not None and current.updated_at != expected_updated_at:
                raise ConfigurationConflict("provider was modified by another request")
            envelope = self._cipher.encrypt(
                credential,
                CredentialAAD(provider_id=provider_id, protocol=current.protocol, field="api_key"),
            )
            row = connection.execute(
                """
                UPDATE provider_connections SET
                    credential_ciphertext = %s, credential_nonce = %s,
                    credential_key_version = %s, credential_fingerprint = %s,
                    updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (
                    envelope.ciphertext,
                    envelope.nonce,
                    envelope.key_version,
                    envelope.fingerprint,
                    provider_id,
                ),
            ).fetchone()
            resource = _provider_from_row(row)
            return self._record_mutation(
                connection,
                resource=resource,
                actor_id=actor_id,
                action="provider.credential_rotated",
                target_type="provider_connection",
                before=current.public_dict(),
                after=resource.public_dict(),
            )

    def soft_delete_provider(
        self,
        provider_id: str,
        *,
        actor_id: str,
    ) -> ConfigurationMutationResult[ProviderConnection]:
        with self._pool.connection() as connection:
            current_row = connection.execute(
                "SELECT * FROM provider_connections WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                (provider_id,),
            ).fetchone()
            if current_row is None:
                raise ConfigurationNotFound(f"provider not found: {provider_id}")
            current = _provider_from_row(current_row)
            row = connection.execute(
                """
                UPDATE provider_connections
                SET status = 'deleted', deleted_at = now(), updated_at = now()
                WHERE id = %s RETURNING *
                """,
                (provider_id,),
            ).fetchone()
            resource = _provider_from_row(row)
            return self._record_mutation(
                connection,
                resource=resource,
                actor_id=actor_id,
                action="provider.deleted",
                target_type="provider_connection",
                before=current.public_dict(),
                after=resource.public_dict(),
            )

    def decrypt_provider_credential(self, provider: ProviderConnection) -> str:
        return self._cipher.decrypt(
            provider.credential,
            CredentialAAD(provider_id=provider.id, protocol=provider.protocol, field="api_key"),
        )

    def create_logical_model(
        self,
        *,
        model_id: str,
        description: str,
        aliases: Iterable[str],
        actor_id: str,
        status: str = "active",
    ) -> ConfigurationMutationResult[LogicalModelRecord]:
        alias_values = _normalize_aliases(aliases, model_id)
        _validate_status(status)
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    INSERT INTO logical_models (id, description, status)
                    VALUES (%s, %s, %s) RETURNING *
                    """,
                    (model_id, description, status),
                ).fetchone()
                for alias in alias_values:
                    connection.execute(
                        "INSERT INTO logical_model_aliases (alias, logical_model_id) VALUES (%s, %s)",
                        (alias, model_id),
                    )
                resource = _logical_model_from_row(row, alias_values)
                return self._record_mutation(
                    connection,
                    resource=resource,
                    actor_id=actor_id,
                    action="logical_model.created",
                    target_type="logical_model",
                    before=None,
                    after=resource.public_dict(),
                )
        except UniqueViolation as error:
            raise ConfigurationConflict("logical model or alias already exists") from error

    def get_logical_model(
        self,
        model_id: str,
        *,
        include_deleted: bool = False,
    ) -> LogicalModelRecord:
        clause = "" if include_deleted else " AND deleted_at IS NULL"
        with self._pool.connection() as connection:
            row = connection.execute(
                f"SELECT * FROM logical_models WHERE id = %s{clause}",
                (model_id,),
            ).fetchone()
            if row is None:
                raise ConfigurationNotFound(f"logical model not found: {model_id}")
            aliases = self._load_aliases(connection, (model_id,)).get(model_id, ())
        return _logical_model_from_row(row, aliases)

    def list_logical_models(self, *, include_deleted: bool = False) -> list[LogicalModelRecord]:
        clause = "" if include_deleted else "WHERE deleted_at IS NULL"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM logical_models {clause} ORDER BY id"
            ).fetchall()
            aliases = self._load_aliases(connection, tuple(row["id"] for row in rows))
        return [_logical_model_from_row(row, aliases.get(row["id"], ())) for row in rows]

    def update_logical_model(
        self,
        model_id: str,
        *,
        actor_id: str,
        description: str | object = _UNSET,
        aliases: Iterable[str] | object = _UNSET,
        status: str | object = _UNSET,
        expected_updated_at: datetime | None = None,
    ) -> ConfigurationMutationResult[LogicalModelRecord]:
        try:
            with self._pool.connection() as connection:
                current_row = connection.execute(
                    "SELECT * FROM logical_models WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                    (model_id,),
                ).fetchone()
                if current_row is None:
                    raise ConfigurationNotFound(f"logical model not found: {model_id}")
                current_aliases = self._load_aliases(connection, (model_id,)).get(model_id, ())
                current = _logical_model_from_row(current_row, current_aliases)
                if expected_updated_at is not None and current.updated_at != expected_updated_at:
                    raise ConfigurationConflict("logical model was modified by another request")
                next_status = current.status if status is _UNSET else str(status)
                _validate_status(next_status)
                next_aliases = current.aliases if aliases is _UNSET else _normalize_aliases(aliases, model_id)
                row = connection.execute(
                    """
                    UPDATE logical_models
                    SET description = %s, status = %s, updated_at = now()
                    WHERE id = %s RETURNING *
                    """,
                    (
                        current.description if description is _UNSET else str(description),
                        next_status,
                        model_id,
                    ),
                ).fetchone()
                if aliases is not _UNSET:
                    connection.execute(
                        "DELETE FROM logical_model_aliases WHERE logical_model_id = %s",
                        (model_id,),
                    )
                    for alias in next_aliases:
                        connection.execute(
                            "INSERT INTO logical_model_aliases (alias, logical_model_id) VALUES (%s, %s)",
                            (alias, model_id),
                        )
                resource = _logical_model_from_row(row, next_aliases)
                return self._record_mutation(
                    connection,
                    resource=resource,
                    actor_id=actor_id,
                    action="logical_model.updated",
                    target_type="logical_model",
                    before=current.public_dict(),
                    after=resource.public_dict(),
                )
        except UniqueViolation as error:
            raise ConfigurationConflict("logical model alias already exists") from error

    def soft_delete_logical_model(
        self,
        model_id: str,
        *,
        actor_id: str,
    ) -> ConfigurationMutationResult[LogicalModelRecord]:
        with self._pool.connection() as connection:
            current_row = connection.execute(
                "SELECT * FROM logical_models WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                (model_id,),
            ).fetchone()
            if current_row is None:
                raise ConfigurationNotFound(f"logical model not found: {model_id}")
            aliases = self._load_aliases(connection, (model_id,)).get(model_id, ())
            current = _logical_model_from_row(current_row, aliases)
            row = connection.execute(
                """
                UPDATE logical_models
                SET status = 'deleted', deleted_at = now(), updated_at = now()
                WHERE id = %s RETURNING *
                """,
                (model_id,),
            ).fetchone()
            resource = _logical_model_from_row(row, aliases)
            return self._record_mutation(
                connection,
                resource=resource,
                actor_id=actor_id,
                action="logical_model.deleted",
                target_type="logical_model",
                before=current.public_dict(),
                after=resource.public_dict(),
            )

    def create_candidate(
        self,
        *,
        candidate_id: str | None = None,
        logical_model_id: str,
        provider_connection_id: str,
        upstream_model_id: str,
        context_window_tokens: int,
        max_output_tokens: int,
        actor_id: str,
        status: str = "active",
        priority: int = 100,
        weight: int = 1,
        input_modalities: Iterable[str] = (),
        output_modalities: Iterable[str] = (),
        tool_modes: Iterable[str] = (),
        schema_support: str = "none",
        streaming_support: bool = False,
        embeddings_support: bool = False,
        retention_class: str = "standard",
        regions: Iterable[str] = (),
        pricing: dict[str, Any] | None = None,
        native_features: Iterable[str] = (),
        unsupported_parameters: Iterable[str] = (),
    ) -> ConfigurationMutationResult[ModelCandidateRecord]:
        _validate_candidate(status, weight, context_window_tokens, max_output_tokens, schema_support)
        candidate_id = candidate_id or f"candidate_{uuid.uuid4().hex}"
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    INSERT INTO model_candidates (
                        id, logical_model_id, provider_connection_id,
                        upstream_model_id, status, priority, weight,
                        context_window_tokens, max_output_tokens,
                        input_modalities, output_modalities, tool_modes,
                        schema_support, streaming_support, embeddings_support,
                        retention_class, regions, pricing, native_features,
                        unsupported_parameters
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    ) RETURNING *
                    """,
                    (
                        candidate_id,
                        logical_model_id,
                        provider_connection_id,
                        upstream_model_id,
                        status,
                        priority,
                        weight,
                        context_window_tokens,
                        max_output_tokens,
                        Jsonb(list(input_modalities)),
                        Jsonb(list(output_modalities)),
                        Jsonb(list(tool_modes)),
                        schema_support,
                        streaming_support,
                        embeddings_support,
                        retention_class,
                        Jsonb(list(regions)),
                        Jsonb(pricing or {}),
                        Jsonb(list(native_features)),
                        Jsonb(list(unsupported_parameters)),
                    ),
                ).fetchone()
                resource = _candidate_from_row(row)
                return self._record_mutation(
                    connection,
                    resource=resource,
                    actor_id=actor_id,
                    action="model_candidate.created",
                    target_type="model_candidate",
                    before=None,
                    after=resource.public_dict(),
                )
        except UniqueViolation as error:
            raise ConfigurationConflict("candidate mapping already exists") from error

    def get_candidate(self, candidate_id: str, *, include_deleted: bool = False) -> ModelCandidateRecord:
        clause = "" if include_deleted else " AND deleted_at IS NULL"
        with self._pool.connection() as connection:
            row = connection.execute(
                f"SELECT * FROM model_candidates WHERE id = %s{clause}",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise ConfigurationNotFound(f"candidate not found: {candidate_id}")
        return _candidate_from_row(row)

    def list_candidates(self, *, include_deleted: bool = False) -> list[ModelCandidateRecord]:
        clause = "" if include_deleted else "WHERE deleted_at IS NULL"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM model_candidates {clause} ORDER BY logical_model_id, priority, id"
            ).fetchall()
        return [_candidate_from_row(row) for row in rows]

    def update_candidate(
        self,
        candidate_id: str,
        *,
        actor_id: str,
        expected_updated_at: datetime | None = None,
        **changes: Any,
    ) -> ConfigurationMutationResult[ModelCandidateRecord]:
        allowed = {
            "logical_model_id", "provider_connection_id", "upstream_model_id", "status",
            "priority", "weight", "context_window_tokens", "max_output_tokens",
            "input_modalities", "output_modalities", "tool_modes", "schema_support",
            "streaming_support", "embeddings_support", "retention_class", "regions",
            "pricing", "native_features", "unsupported_parameters",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported candidate fields: {', '.join(sorted(unknown))}")
        try:
            with self._pool.connection() as connection:
                current_row = connection.execute(
                    "SELECT * FROM model_candidates WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                    (candidate_id,),
                ).fetchone()
                if current_row is None:
                    raise ConfigurationNotFound(f"candidate not found: {candidate_id}")
                current = _candidate_from_row(current_row)
                if expected_updated_at is not None and current.updated_at != expected_updated_at:
                    raise ConfigurationConflict("candidate was modified by another request")
                values = current.public_dict()
                values.update(changes)
                _validate_candidate(
                    str(values["status"]),
                    int(values["weight"]),
                    int(values["context_window_tokens"]),
                    int(values["max_output_tokens"]),
                    str(values["schema_support"]),
                )
                row = connection.execute(
                    """
                    UPDATE model_candidates SET
                        logical_model_id = %s, provider_connection_id = %s,
                        upstream_model_id = %s, status = %s, priority = %s,
                        weight = %s, context_window_tokens = %s,
                        max_output_tokens = %s, input_modalities = %s,
                        output_modalities = %s, tool_modes = %s,
                        schema_support = %s, streaming_support = %s,
                        embeddings_support = %s, retention_class = %s,
                        regions = %s, pricing = %s, native_features = %s,
                        unsupported_parameters = %s, updated_at = now()
                    WHERE id = %s RETURNING *
                    """,
                    (
                        values["logical_model_id"], values["provider_connection_id"],
                        values["upstream_model_id"], values["status"], values["priority"],
                        values["weight"], values["context_window_tokens"],
                        values["max_output_tokens"], Jsonb(list(values["input_modalities"])),
                        Jsonb(list(values["output_modalities"])), Jsonb(list(values["tool_modes"])),
                        values["schema_support"], values["streaming_support"],
                        values["embeddings_support"], values["retention_class"],
                        Jsonb(list(values["regions"])), Jsonb(values["pricing"]),
                        Jsonb(list(values["native_features"])),
                        Jsonb(list(values["unsupported_parameters"])), candidate_id,
                    ),
                ).fetchone()
                resource = _candidate_from_row(row)
                return self._record_mutation(
                    connection,
                    resource=resource,
                    actor_id=actor_id,
                    action="model_candidate.updated",
                    target_type="model_candidate",
                    before=current.public_dict(),
                    after=resource.public_dict(),
                )
        except UniqueViolation as error:
            raise ConfigurationConflict("candidate mapping already exists") from error

    def soft_delete_candidate(
        self,
        candidate_id: str,
        *,
        actor_id: str,
    ) -> ConfigurationMutationResult[ModelCandidateRecord]:
        with self._pool.connection() as connection:
            current_row = connection.execute(
                "SELECT * FROM model_candidates WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                (candidate_id,),
            ).fetchone()
            if current_row is None:
                raise ConfigurationNotFound(f"candidate not found: {candidate_id}")
            current = _candidate_from_row(current_row)
            row = connection.execute(
                """
                UPDATE model_candidates
                SET status = 'deleted', deleted_at = now(), updated_at = now()
                WHERE id = %s RETURNING *
                """,
                (candidate_id,),
            ).fetchone()
            resource = _candidate_from_row(row)
            return self._record_mutation(
                connection,
                resource=resource,
                actor_id=actor_id,
                action="model_candidate.deleted",
                target_type="model_candidate",
                before=current.public_dict(),
                after=resource.public_dict(),
            )

    def load_active_configuration(self) -> StoredConfiguration:
        with self._pool.connection() as connection:
            return self._load_active_configuration(connection)

    def load_publication_configuration(
        self,
        version: int,
    ) -> tuple[ConfigurationVersion, StoredConfiguration]:
        with self._pool.connection() as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            version_row = connection.execute(
                "SELECT * FROM configuration_versions WHERE version = %s",
                (version,),
            ).fetchone()
            if version_row is None:
                raise ConfigurationNotFound(f"configuration version not found: {version}")
            configuration = self._load_active_configuration(connection)
        return _version_from_row(version_row), configuration

    def latest_published_version(self) -> ConfigurationVersion | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM configuration_versions
                WHERE status = 'published'
                ORDER BY version DESC LIMIT 1
                """
            ).fetchone()
        return _version_from_row(row) if row is not None else None

    def latest_configuration_version(self) -> ConfigurationVersion | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM configuration_versions ORDER BY version DESC LIMIT 1"
            ).fetchone()
        return _version_from_row(row) if row is not None else None

    def get_configuration_version(self, version: int) -> ConfigurationVersion:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM configuration_versions WHERE version = %s",
                (version,),
            ).fetchone()
        if row is None:
            raise ConfigurationNotFound(f"configuration version not found: {version}")
        return _version_from_row(row)

    def count_active_candidates(
        self,
        logical_model_id: str,
        *,
        exclude_candidate_id: str | None = None,
    ) -> int:
        exclusion = "" if exclude_candidate_id is None else "AND id <> %s"
        parameters: tuple[Any, ...] = (
            (logical_model_id,)
            if exclude_candidate_id is None
            else (logical_model_id, exclude_candidate_id)
        )
        with self._pool.connection() as connection:
            row = connection.execute(
                f"""
                SELECT count(*) AS count FROM model_candidates
                WHERE logical_model_id = %s AND status = 'active'
                  AND deleted_at IS NULL
                  {exclusion}
                """,
                parameters,
            ).fetchone()
        return row["count"]

    def mark_configuration_published(
        self,
        version: int,
        checksum: str,
        *,
        event_id: int | None = None,
        worker_id: str | None = None,
    ) -> bool:
        with self._pool.connection() as connection:
            if event_id is not None and not self._owns_outbox_claim(
                connection,
                event_id,
                version,
                worker_id,
            ):
                return False
            row = connection.execute(
                """
                UPDATE configuration_versions
                SET status = 'published', checksum = %s, published_at = now(),
                    error_code = NULL, error_message = NULL, failed_at = NULL
                WHERE version = %s
                RETURNING version
                """,
                (checksum, version),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """
                UPDATE configuration_outbox
                SET processed_at = COALESCE(processed_at, now()),
                    claimed_by = NULL, claimed_at = NULL,
                    claim_expires_at = NULL, last_error = NULL
                WHERE configuration_version = %s
                """,
                (version,),
            )
        return True

    def mark_configuration_failed(
        self,
        version: int,
        *,
        error_code: str,
        error_message: str,
        event_id: int | None = None,
        worker_id: str | None = None,
    ) -> bool:
        safe_code = error_code[:100]
        safe_message = error_message[:500]
        with self._pool.connection() as connection:
            if event_id is not None and not self._owns_outbox_claim(
                connection,
                event_id,
                version,
                worker_id,
            ):
                return False
            row = connection.execute(
                """
                UPDATE configuration_versions
                SET status = 'failed', error_code = %s, error_message = %s,
                    failed_at = now()
                WHERE version = %s
                RETURNING version
                """,
                (safe_code, safe_message, version),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """
                UPDATE configuration_outbox
                SET processed_at = COALESCE(processed_at, now()),
                    claimed_by = NULL, claimed_at = NULL,
                    claim_expires_at = NULL, last_error = %s
                WHERE configuration_version = %s
                """,
                (safe_message, version),
            )
        return True

    def _load_active_configuration(self, connection: Connection) -> StoredConfiguration:
        provider_rows = connection.execute(
            """
            SELECT * FROM provider_connections
            WHERE status = 'active' AND deleted_at IS NULL
            ORDER BY name, id
            """
        ).fetchall()
        model_rows = connection.execute(
            """
            SELECT * FROM logical_models
            WHERE status = 'active' AND deleted_at IS NULL
            ORDER BY id
            """
        ).fetchall()
        model_ids = tuple(row["id"] for row in model_rows)
        aliases = self._load_aliases(connection, model_ids)
        candidate_rows = connection.execute(
            """
            SELECT candidate.*
            FROM model_candidates AS candidate
            JOIN logical_models AS model ON model.id = candidate.logical_model_id
            JOIN provider_connections AS provider
              ON provider.id = candidate.provider_connection_id
            WHERE candidate.status = 'active' AND candidate.deleted_at IS NULL
              AND model.status = 'active' AND model.deleted_at IS NULL
              AND provider.status = 'active' AND provider.deleted_at IS NULL
            ORDER BY candidate.logical_model_id, candidate.priority, candidate.id
            """
        ).fetchall()
        return StoredConfiguration(
            providers=tuple(_provider_from_row(row) for row in provider_rows),
            logical_models=tuple(
                _logical_model_from_row(row, aliases.get(row["id"], ())) for row in model_rows
            ),
            candidates=tuple(_candidate_from_row(row) for row in candidate_rows),
            aliases={alias: model_id for model_id, values in aliases.items() for alias in values},
        )

    @staticmethod
    def _owns_outbox_claim(
        connection: Connection,
        event_id: int,
        version: int,
        worker_id: str | None,
    ) -> bool:
        if not worker_id:
            return False
        row = connection.execute(
            """
            SELECT id FROM configuration_outbox
            WHERE id = %s AND configuration_version = %s
              AND claimed_by = %s AND processed_at IS NULL
              AND claim_expires_at >= now()
            FOR UPDATE
            """,
            (event_id, version, worker_id),
        ).fetchone()
        return row is not None

    def claim_outbox(
        self,
        worker_id: str,
        *,
        limit: int = 10,
        lease_seconds: int = 30,
    ) -> list[ConfigurationOutboxEvent]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                WITH candidates AS (
                    SELECT id FROM configuration_outbox
                    WHERE processed_at IS NULL
                      AND (claim_expires_at IS NULL OR claim_expires_at < now())
                    ORDER BY id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE configuration_outbox AS outbox
                SET claimed_by = %s, claimed_at = now(),
                    claim_expires_at = now() + %s,
                    attempts = attempts + 1, last_error = NULL
                FROM candidates
                WHERE outbox.id = candidates.id
                RETURNING outbox.*
                """,
                (limit, worker_id, timedelta(seconds=lease_seconds)),
            ).fetchall()
        return [_outbox_from_row(row) for row in rows]

    def renew_outbox_claim(self, event_id: int, worker_id: str, *, lease_seconds: int = 30) -> bool:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE configuration_outbox
                SET claim_expires_at = now() + %s
                WHERE id = %s AND claimed_by = %s AND processed_at IS NULL
                  AND claim_expires_at >= now()
                RETURNING id
                """,
                (timedelta(seconds=lease_seconds), event_id, worker_id),
            ).fetchone()
        return row is not None

    def complete_outbox(self, event_id: int, worker_id: str) -> bool:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE configuration_outbox
                SET processed_at = now(), claim_expires_at = NULL
                WHERE id = %s AND claimed_by = %s AND processed_at IS NULL
                  AND claim_expires_at >= now()
                RETURNING configuration_version
                """,
                (event_id, worker_id),
            ).fetchone()
            if row is not None:
                connection.execute(
                    """
                    UPDATE configuration_versions
                    SET status = 'published', published_at = now(),
                        error_code = NULL, error_message = NULL, failed_at = NULL
                    WHERE version = %s
                    """,
                    (row["configuration_version"],),
                )
        return row is not None

    def release_outbox(self, event_id: int, worker_id: str, *, error: str) -> bool:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE configuration_outbox
                SET claimed_by = NULL, claimed_at = NULL, claim_expires_at = NULL,
                    last_error = %s
                WHERE id = %s AND claimed_by = %s AND processed_at IS NULL
                RETURNING id
                """,
                (error[:500], event_id, worker_id),
            ).fetchone()
        return row is not None

    def record_publication_failure(
        self,
        version: int,
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                UPDATE configuration_versions
                SET status = 'failed', error_code = %s, error_message = %s,
                    failed_at = now()
                WHERE version = %s
                """,
                (error_code[:100], error_message[:500], version),
            )

    def _record_mutation(
        self,
        connection: Connection,
        *,
        resource: Any,
        actor_id: str,
        action: str,
        target_type: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> ConfigurationMutationResult[Any]:
        version_row = connection.execute(
            "INSERT INTO configuration_versions DEFAULT VALUES RETURNING *"
        ).fetchone()
        audit_id = f"audit_{uuid.uuid4().hex}"
        connection.execute(
            """
            INSERT INTO audit_events (
                id, actor_id, tenant_id, action, target_type, target_id, before, after
            ) VALUES (%s, %s, 'global', %s, %s, %s, %s, %s)
            """,
            (
                audit_id,
                actor_id,
                action,
                target_type,
                resource.id,
                Jsonb(before) if before is not None else None,
                Jsonb(after) if after is not None else None,
            ),
        )
        outbox_row = connection.execute(
            """
            INSERT INTO configuration_outbox (
                configuration_version, event_type, payload
            ) VALUES (%s, 'configuration.changed', %s)
            RETURNING id
            """,
            (
                version_row["version"],
                Jsonb(
                    {
                        "action": action,
                        "target_type": target_type,
                        "target_id": resource.id,
                    }
                ),
            ),
        ).fetchone()
        return ConfigurationMutationResult(
            resource=resource,
            version=_version_from_row(version_row),
            audit_id=audit_id,
            outbox_id=outbox_row["id"],
        )

    @staticmethod
    def _load_aliases(connection: Connection, model_ids: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
        if not model_ids:
            return {}
        rows = connection.execute(
            """
            SELECT alias, logical_model_id FROM logical_model_aliases
            WHERE logical_model_id = ANY(%s)
            ORDER BY alias
            """,
            (list(model_ids),),
        ).fetchall()
        values: dict[str, list[str]] = {model_id: [] for model_id in model_ids}
        for row in rows:
            values[row["logical_model_id"]].append(row["alias"])
        return {model_id: tuple(aliases) for model_id, aliases in values.items()}


def _provider_from_row(row: dict[str, Any]) -> ProviderConnection:
    return ProviderConnection(
        id=row["id"],
        name=row["name"],
        protocol=row["protocol"],
        base_url=row["base_url"],
        credential=EncryptedCredential(
            key_version=row["credential_key_version"],
            nonce=bytes(row["credential_nonce"]),
            ciphertext=bytes(row["credential_ciphertext"]),
            fingerprint=row["credential_fingerprint"],
        ),
        timeout_seconds=row["timeout_seconds"],
        status=row["status"],
        priority=row["priority"],
        weight=row["weight"],
        metadata=dict(row["metadata"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


def _logical_model_from_row(row: dict[str, Any], aliases: Iterable[str]) -> LogicalModelRecord:
    return LogicalModelRecord(
        id=row["id"],
        description=row["description"],
        status=row["status"],
        aliases=tuple(aliases),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


def _candidate_from_row(row: dict[str, Any]) -> ModelCandidateRecord:
    return ModelCandidateRecord(
        id=row["id"],
        logical_model_id=row["logical_model_id"],
        provider_connection_id=row["provider_connection_id"],
        upstream_model_id=row["upstream_model_id"],
        status=row["status"],
        priority=row["priority"],
        weight=row["weight"],
        context_window_tokens=row["context_window_tokens"],
        max_output_tokens=row["max_output_tokens"],
        input_modalities=tuple(row["input_modalities"]),
        output_modalities=tuple(row["output_modalities"]),
        tool_modes=tuple(row["tool_modes"]),
        schema_support=row["schema_support"],
        streaming_support=row["streaming_support"],
        embeddings_support=row["embeddings_support"],
        retention_class=row["retention_class"],
        regions=tuple(row["regions"]),
        pricing=dict(row["pricing"]),
        native_features=tuple(row["native_features"]),
        unsupported_parameters=tuple(row["unsupported_parameters"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


def _version_from_row(row: dict[str, Any]) -> ConfigurationVersion:
    return ConfigurationVersion(
        version=row["version"],
        status=row["status"],
        checksum=row["checksum"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        published_at=row["published_at"],
        failed_at=row["failed_at"],
    )


def _outbox_from_row(row: dict[str, Any]) -> ConfigurationOutboxEvent:
    return ConfigurationOutboxEvent(
        id=row["id"],
        configuration_version=row["configuration_version"],
        event_type=row["event_type"],
        payload=dict(row["payload"]),
        claimed_by=row["claimed_by"],
        claimed_at=row["claimed_at"],
        claim_expires_at=row["claim_expires_at"],
        processed_at=row["processed_at"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        created_at=row["created_at"],
    )


def _validate_provider(
    protocol: str,
    base_url: str,
    timeout_seconds: Decimal,
    status: str,
    weight: int,
    allow_private: bool,
) -> None:
    if protocol not in _PROVIDER_PROTOCOLS:
        raise ValueError("unsupported provider protocol")
    _validate_base_url(base_url, allow_private=allow_private)
    if Decimal(timeout_seconds) <= 0:
        raise ValueError("provider timeout must be positive")
    _validate_status(status)
    if weight <= 0:
        raise ValueError("provider weight must be positive")


def _validate_base_url(value: str, *, allow_private: bool) -> None:
    if not value or len(value) > 2048:
        raise ValueError("invalid provider base URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("invalid provider base URL")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("invalid provider base URL")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("invalid provider base URL")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not allow_private and not address.is_global:
        raise ValueError("invalid provider base URL")


def _validate_status(status: str) -> None:
    if status not in {"active", "disabled", "deleted"}:
        raise ValueError("invalid configuration status")


def _validate_candidate(
    status: str,
    weight: int,
    context_window_tokens: int,
    max_output_tokens: int,
    schema_support: str,
) -> None:
    _validate_status(status)
    if weight <= 0:
        raise ValueError("candidate weight must be positive")
    if context_window_tokens <= 0 or max_output_tokens < 0:
        raise ValueError("candidate token limits are invalid")
    if schema_support not in _SCHEMA_SUPPORT:
        raise ValueError("candidate schema support is invalid")


def _normalize_aliases(aliases: Iterable[str], model_id: str) -> tuple[str, ...]:
    values = tuple(sorted(set(aliases)))
    if any(not value or len(value) > 200 for value in values):
        raise ValueError("logical model alias is invalid")
    if model_id in values:
        raise ValueError("logical model alias cannot equal its model ID")
    return values
