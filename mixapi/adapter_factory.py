from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from mixapi.adapters import (
    AnthropicProviderAdapter,
    GeminiProviderAdapter,
    OllamaProviderAdapter,
    OpenAICompatibleProviderAdapter,
)
from mixapi.configuration import ProviderConnection
from mixapi.models import ProviderModel
from mixapi.secrets import CredentialAAD, CredentialCipher
from mixapi.snapshots import ConfigurationSnapshot


class AdapterFactory:
    def __init__(
        self,
        cipher: CredentialCipher,
        *,
        deterministic_adapter: Any | None = None,
        connection_overrides: dict[str, Any] | None = None,
        anthropic_version: str = "2023-06-01",
        retained_versions: int = 3,
    ) -> None:
        self._cipher = cipher
        self._deterministic_adapter = deterministic_adapter
        self._connection_overrides = dict(connection_overrides or {})
        self._anthropic_version = anthropic_version
        self._retained_versions = retained_versions
        self._cache: dict[tuple[int, str, str], Any] = {}

    def for_snapshot(self, snapshot: ConfigurationSnapshot) -> SnapshotAdapterResolver:
        self._evict(snapshot.version)
        return SnapshotAdapterResolver(self, snapshot)

    def for_connection(self, snapshot_version: int, provider: ProviderConnection) -> Any:
        key = (snapshot_version, provider.id, provider.updated_at.isoformat())
        if key in self._cache:
            return self._cache[key]
        adapter = self._build(provider)
        self._cache[key] = adapter
        return adapter

    def _build(self, provider: ProviderConnection) -> Any:
        if provider.id in self._connection_overrides:
            return self._connection_overrides[provider.id]
        hostname = (urlsplit(provider.base_url).hostname or "").lower()
        if self._deterministic_adapter is not None and (
            provider.metadata.get("adapter") == "deterministic"
            or hostname.endswith(".example")
        ):
            return self._deterministic_adapter
        credential = self._cipher.decrypt(
            provider.credential,
            CredentialAAD(
                provider_id=provider.id,
                protocol=provider.protocol,
                field="api_key",
            ),
        )
        timeout = float(provider.timeout_seconds)
        if provider.protocol == "openai-compatible":
            return OpenAICompatibleProviderAdapter(provider.base_url, credential, timeout)
        if provider.protocol == "anthropic":
            return AnthropicProviderAdapter(
                provider.base_url,
                credential,
                self._anthropic_version,
                timeout,
            )
        if provider.protocol == "gemini":
            return GeminiProviderAdapter(provider.base_url, credential, timeout)
        if provider.protocol == "ollama":
            return OllamaProviderAdapter(provider.base_url, credential, timeout)
        raise ValueError(f"Unsupported provider protocol: {provider.protocol}")

    def _evict(self, active_version: int) -> None:
        versions = sorted({key[0] for key in self._cache}, reverse=True)
        keep = set([active_version, *versions[: self._retained_versions - 1]])
        self._cache = {key: value for key, value in self._cache.items() if key[0] in keep}


@dataclass(frozen=True)
class SnapshotAdapterResolver:
    factory: AdapterFactory
    snapshot: ConfigurationSnapshot

    def for_candidate(self, candidate: ProviderModel) -> Any:
        provider_id = candidate.provider_connection_id
        if provider_id is None:
            raise ValueError("Candidate has no provider connection ID")
        provider = next(
            (item for item in self.snapshot.providers if item.id == provider_id),
            None,
        )
        if provider is None:
            raise ValueError(f"Provider connection not found: {provider_id}")
        return self.factory.for_connection(self.snapshot.version, provider)
