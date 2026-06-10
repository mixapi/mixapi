from __future__ import annotations

import ipaddress
from typing import Protocol
from urllib.parse import urlsplit

from mixapi.configuration import (
    ConfigurationVersion,
    ProviderConnection,
    StoredConfiguration,
)
from mixapi.errors import MixAPIError
from mixapi.runtime.snapshots import RedisSnapshotStore, SnapshotConflict
from mixapi.secrets import InvalidCredentialCiphertext
from mixapi.snapshots import ConfigurationSnapshot, SnapshotValidationError


class PublicationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PublicationLeaseLost(RuntimeError):
    pass


class ConfigurationRepository(Protocol):
    def load_publication_configuration(
        self,
        version: int,
    ) -> tuple[ConfigurationVersion, StoredConfiguration]: ...

    def latest_published_version(self) -> ConfigurationVersion | None: ...

    def decrypt_provider_credential(self, provider: ProviderConnection) -> str: ...

    def mark_configuration_published(
        self,
        version: int,
        checksum: str,
        *,
        event_id: int | None = None,
        worker_id: str | None = None,
    ) -> bool: ...

    def mark_configuration_failed(
        self,
        version: int,
        *,
        error_code: str,
        error_message: str,
        event_id: int | None = None,
        worker_id: str | None = None,
    ) -> bool: ...


class ConfigurationPublisher:
    def __init__(
        self,
        repository: ConfigurationRepository,
        snapshots: RedisSnapshotStore,
        *,
        retention_count: int,
        retention_ttl_seconds: int,
    ) -> None:
        self._repository = repository
        self._snapshots = snapshots
        self._retention_count = retention_count
        self._retention_ttl_seconds = retention_ttl_seconds

    def publish(
        self,
        version: int,
        *,
        event_id: int | None = None,
        worker_id: str | None = None,
    ) -> ConfigurationSnapshot:
        try:
            version_record, configuration = self._repository.load_publication_configuration(
                version
            )
            self._validate(configuration)
            snapshot = ConfigurationSnapshot.create(
                version_record.version,
                version_record.created_at,
                configuration,
            )
            self._publish_snapshot(snapshot)
        except PublicationError as error:
            marked = self._repository.mark_configuration_failed(
                version,
                error_code=error.code,
                error_message=error.message,
                event_id=event_id,
                worker_id=worker_id,
            )
            if event_id is not None and not marked:
                raise PublicationLeaseLost(
                    f"Publication lease was lost for configuration version {version}"
                ) from error
            raise
        except SnapshotValidationError as error:
            publication_error = PublicationError(
                "invalid_configuration_snapshot",
                "The configuration could not be encoded as a valid snapshot.",
            )
            marked = self._repository.mark_configuration_failed(
                version,
                error_code=publication_error.code,
                error_message=publication_error.message,
                event_id=event_id,
                worker_id=worker_id,
            )
            if event_id is not None and not marked:
                raise PublicationLeaseLost(
                    f"Publication lease was lost for configuration version {version}"
                ) from error
            raise publication_error from error

        marked = self._repository.mark_configuration_published(
            version,
            snapshot.checksum,
            event_id=event_id,
            worker_id=worker_id,
        )
        if event_id is not None and not marked:
            raise PublicationLeaseLost(
                f"Publication lease was lost for configuration version {version}"
            )
        return snapshot

    def rebuild(self) -> bool:
        version = self._repository.latest_published_version()
        if version is None:
            return False
        active_version = self._snapshots.active_version()
        try:
            existing = self._snapshots.load(version.version)
        except MixAPIError as error:
            if error.code != "configuration_unavailable":
                raise
        else:
            if active_version == version.version:
                return False
            self._snapshots.mark_ready(existing.version, existing.checksum)
            self._snapshots.activate(existing.version, existing.checksum)
            return True

        version_record, configuration = self._repository.load_publication_configuration(
            version.version
        )
        self._validate(configuration)
        snapshot = ConfigurationSnapshot.create(
            version_record.version,
            version_record.created_at,
            configuration,
        )
        self._publish_snapshot(snapshot)
        return True

    def _publish_snapshot(self, snapshot: ConfigurationSnapshot) -> None:
        try:
            self._snapshots.write_pending(snapshot)
            self._snapshots.mark_ready(snapshot.version, snapshot.checksum)
            self._snapshots.activate(snapshot.version, snapshot.checksum)
            self._snapshots.retain_versions(
                active_version=snapshot.version,
                count=self._retention_count,
                ttl_seconds=self._retention_ttl_seconds,
            )
        except SnapshotConflict as error:
            raise PublicationError(
                "snapshot_conflict",
                "The configuration version conflicts with an existing snapshot.",
            ) from error

    def _validate(self, configuration: StoredConfiguration) -> None:
        if not configuration.providers or not configuration.logical_models:
            raise PublicationError(
                "incomplete_configuration",
                "Routing configuration requires providers and logical models.",
            )
        provider_ids = {provider.id for provider in configuration.providers}
        model_ids = {model.id for model in configuration.logical_models}
        expected_aliases = {
            alias: model.id
            for model in configuration.logical_models
            for alias in model.aliases
        }
        if configuration.aliases != expected_aliases or any(
            alias in model_ids for alias in configuration.aliases
        ):
            raise PublicationError(
                "invalid_alias",
                "Logical model aliases are inconsistent or ambiguous.",
            )

        candidates_by_model = {model_id: 0 for model_id in model_ids}
        for candidate in configuration.candidates:
            if candidate.logical_model_id not in model_ids:
                raise PublicationError(
                    "invalid_model_candidate",
                    "A model candidate references an unavailable logical model.",
                )
            if candidate.provider_connection_id not in provider_ids:
                raise PublicationError(
                    "invalid_model_candidate",
                    "A model candidate references an unavailable provider connection.",
                )
            candidates_by_model[candidate.logical_model_id] += 1
        if any(count == 0 for count in candidates_by_model.values()):
            raise PublicationError(
                "missing_model_candidate",
                "Every active logical model must have at least one active candidate.",
            )

        for provider in configuration.providers:
            self._validate_provider(provider)

    def _validate_provider(self, provider: ProviderConnection) -> None:
        if provider.protocol not in {
            "openai-compatible",
            "anthropic",
            "gemini",
            "ollama",
        }:
            raise PublicationError(
                "unsupported_provider_protocol",
                "A provider connection uses an unsupported protocol.",
            )
        if not _is_safe_provider_url(provider.base_url):
            raise PublicationError(
                "invalid_provider_url",
                "A provider connection has an invalid base URL.",
            )
        try:
            self._repository.decrypt_provider_credential(provider)
        except (InvalidCredentialCiphertext, UnicodeDecodeError, ValueError) as error:
            raise PublicationError(
                "invalid_provider_credential",
                "A provider connection has an undecryptable credential.",
            ) from error


def _is_safe_provider_url(value: str) -> bool:
    if not value or len(value) > 2048:
        return False
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
