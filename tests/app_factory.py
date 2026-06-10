from __future__ import annotations

import os
from typing import Any

from mixapi.adapters import (
    AnthropicProviderAdapter,
    DeterministicProviderAdapter,
    GeminiProviderAdapter,
    OllamaProviderAdapter,
    OpenAICompatibleProviderAdapter,
)
from mixapi.app import create_app as create_runtime_app


def create_app(
    *,
    failed_response_providers: set[str] | None = None,
    openai_base_url: str | None = None,
    openai_api_key: str | None = None,
    anthropic_base_url: str | None = None,
    anthropic_api_key: str | None = None,
    anthropic_version: str | None = None,
    gemini_base_url: str | None = None,
    gemini_api_key: str | None = None,
    ollama_base_url: str | None = None,
    ollama_api_key: str | None = None,
    provider_timeout_seconds: float = 30.0,
    adapter_overrides: dict[str, Any] | None = None,
    **kwargs: Any,
):
    deterministic = DeterministicProviderAdapter(
        failed_response_providers=failed_response_providers
    )
    overrides: dict[str, Any] = {
        "provider_openai": deterministic,
        "provider_anthropic": deterministic,
        "provider_gemini": deterministic,
        "provider_ollama": deterministic,
    }

    if endpoint := openai_base_url or os.getenv("MIXAPI_OPENAI_BASE_URL"):
        overrides["provider_openai"] = OpenAICompatibleProviderAdapter(
            endpoint,
            openai_api_key or os.getenv("MIXAPI_OPENAI_API_KEY"),
            provider_timeout_seconds,
        )
    if endpoint := anthropic_base_url or os.getenv("MIXAPI_ANTHROPIC_BASE_URL"):
        overrides["provider_anthropic"] = AnthropicProviderAdapter(
            endpoint,
            anthropic_api_key or os.getenv("MIXAPI_ANTHROPIC_API_KEY"),
            anthropic_version or os.getenv("MIXAPI_ANTHROPIC_VERSION", "2023-06-01"),
            provider_timeout_seconds,
        )
    if endpoint := gemini_base_url or os.getenv("MIXAPI_GEMINI_BASE_URL"):
        overrides["provider_gemini"] = GeminiProviderAdapter(
            endpoint,
            gemini_api_key or os.getenv("MIXAPI_GEMINI_API_KEY"),
            provider_timeout_seconds,
        )
    if endpoint := ollama_base_url or os.getenv("MIXAPI_OLLAMA_BASE_URL"):
        overrides["provider_ollama"] = OllamaProviderAdapter(
            endpoint,
            ollama_api_key or os.getenv("MIXAPI_OLLAMA_API_KEY"),
            provider_timeout_seconds,
        )
    overrides.update(adapter_overrides or {})
    return create_runtime_app(adapter_overrides=overrides, **kwargs)
