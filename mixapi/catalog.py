from __future__ import annotations

from typing import Any

from mixapi.models import LogicalModel, ProviderModel
from mixapi.snapshots import ConfigurationSnapshot


def default_catalog() -> dict[str, LogicalModel]:
    chat_candidates = (
        ProviderModel(
            id="openai:gpt-4.1-mini",
            provider="openai",
            provider_model_id="gpt-4.1-mini",
            logical_model_id="mixapi/balanced-chat",
            status="active",
            context_window_tokens=128_000,
            max_output_tokens=16_384,
            input_modalities=("text", "image"),
            output_modalities=("text",),
            tool_modes=("function",),
            schema_support="strict_json_schema",
            streaming_support=True,
            embeddings_support=False,
            retention_class="standard",
            regions=("us", "eu"),
            pricing={"input_per_million": "0.40", "output_per_million": "1.60"},
            native_features=("responses",),
        ),
        ProviderModel(
            id="anthropic:claude-sonnet-4",
            provider="anthropic",
            provider_model_id="claude-sonnet-4",
            logical_model_id="mixapi/balanced-chat",
            status="active",
            context_window_tokens=200_000,
            max_output_tokens=16_384,
            input_modalities=("text", "image"),
            output_modalities=("text",),
            tool_modes=("function",),
            schema_support="best_effort_schema",
            streaming_support=True,
            embeddings_support=False,
            retention_class="standard",
            regions=("us",),
            pricing={"input_per_million": "3.00", "output_per_million": "15.00"},
            native_features=("messages",),
        ),
        ProviderModel(
            id="gemini:gemini-2.5-flash",
            provider="gemini",
            provider_model_id="gemini-2.5-flash",
            logical_model_id="mixapi/balanced-chat",
            status="active",
            context_window_tokens=1_000_000,
            max_output_tokens=65_536,
            input_modalities=("text",),
            output_modalities=("text",),
            tool_modes=("function",),
            schema_support="strict_json_schema",
            streaming_support=True,
            embeddings_support=False,
            retention_class="standard",
            regions=("us", "eu"),
            pricing={"input_per_million": "0.30", "output_per_million": "2.50"},
            native_features=("generate_content",),
        ),
        ProviderModel(
            id="ollama:llama3.1",
            provider="ollama",
            provider_model_id="llama3.1",
            logical_model_id="mixapi/balanced-chat",
            status="active",
            context_window_tokens=32_000,
            max_output_tokens=8_192,
            input_modalities=("text",),
            output_modalities=("text",),
            tool_modes=(),
            schema_support="json_mode",
            streaming_support=True,
            embeddings_support=False,
            retention_class="local",
            regions=("local",),
            pricing={"input_per_million": "0.00", "output_per_million": "0.00"},
            native_features=("local",),
        ),
    )
    embedding_candidates = (
        ProviderModel(
            id="openai:text-embedding-3-small",
            provider="openai",
            provider_model_id="text-embedding-3-small",
            logical_model_id="mixapi/embedding-small",
            status="active",
            context_window_tokens=8_191,
            max_output_tokens=0,
            input_modalities=("text",),
            output_modalities=("embedding",),
            tool_modes=(),
            schema_support="none",
            streaming_support=False,
            embeddings_support=True,
            retention_class="standard",
            regions=("us", "eu"),
            pricing={"input_per_million": "0.02"},
            native_features=("embeddings",),
        ),
        ProviderModel(
            id="ollama:nomic-embed-text",
            provider="ollama",
            provider_model_id="nomic-embed-text",
            logical_model_id="mixapi/embedding-small",
            status="active",
            context_window_tokens=8_192,
            max_output_tokens=0,
            input_modalities=("text",),
            output_modalities=("embedding",),
            tool_modes=(),
            schema_support="none",
            streaming_support=False,
            embeddings_support=True,
            retention_class="local",
            regions=("local",),
            pricing={"input_per_million": "0.00"},
            native_features=("local", "embeddings"),
        ),
    )
    return {
        "mixapi/balanced-chat": LogicalModel(
            id="mixapi/balanced-chat",
            status="active",
            description="Balanced multi-provider text model",
            candidates=chat_candidates,
        ),
        "mixapi/embedding-small": LogicalModel(
            id="mixapi/embedding-small",
            status="active",
            description="Small embedding model",
            candidates=embedding_candidates,
        ),
    }


def catalog_from_snapshot(snapshot: ConfigurationSnapshot) -> dict[str, LogicalModel]:
    providers = {provider.id: provider for provider in snapshot.providers}
    candidates_by_model: dict[str, list[ProviderModel]] = {
        model.id: [] for model in snapshot.logical_models
    }
    for candidate in sorted(
        snapshot.candidates,
        key=lambda item: (item.logical_model_id, item.priority, item.id),
    ):
        provider = providers[candidate.provider_connection_id]
        candidates_by_model[candidate.logical_model_id].append(
            ProviderModel(
                id=candidate.id,
                provider=_runtime_provider_name(provider.protocol),
                provider_model_id=candidate.upstream_model_id,
                logical_model_id=candidate.logical_model_id,
                status=candidate.status,
                context_window_tokens=candidate.context_window_tokens,
                max_output_tokens=candidate.max_output_tokens,
                input_modalities=candidate.input_modalities,
                output_modalities=candidate.output_modalities,
                tool_modes=candidate.tool_modes,
                schema_support=candidate.schema_support,
                streaming_support=candidate.streaming_support,
                embeddings_support=candidate.embeddings_support,
                retention_class=candidate.retention_class,
                regions=candidate.regions,
                pricing=candidate.pricing,
                native_features=candidate.native_features,
                unsupported_parameters=candidate.unsupported_parameters,
                provider_connection_id=candidate.provider_connection_id,
                priority=candidate.priority,
                weight=candidate.weight,
            )
        )
    return {
        model.id: LogicalModel(
            id=model.id,
            status=model.status,
            description=model.description,
            candidates=tuple(candidates_by_model[model.id]),
        )
        for model in snapshot.logical_models
    }


def default_seed_document(*, credential: str) -> dict[str, Any]:
    provider_specs = {
        "openai": {
            "id": "provider_openai",
            "name": "OpenAI default",
            "protocol": "openai-compatible",
            "base_url": "https://api.openai.com/v1",
        },
        "anthropic": {
            "id": "provider_anthropic",
            "name": "Anthropic default",
            "protocol": "anthropic",
            "base_url": "https://api.anthropic.com",
        },
        "gemini": {
            "id": "provider_gemini",
            "name": "Gemini default",
            "protocol": "gemini",
            "base_url": "https://generativelanguage.googleapis.com",
        },
        "ollama": {
            "id": "provider_ollama",
            "name": "Ollama default",
            "protocol": "ollama",
            "base_url": "https://ollama.example",
        },
    }
    catalog = default_catalog()
    return {
        "schema_version": 1,
        "providers": [
            {
                **spec,
                "credential": credential,
                "timeout_seconds": "30",
                "status": "active",
                "priority": 100,
                "weight": 1,
                "metadata": {},
            }
            for spec in provider_specs.values()
        ],
        "logical_models": [
            {
                "id": model.id,
                "description": model.description,
                "aliases": [],
                "status": model.status,
            }
            for model in catalog.values()
        ],
        "candidates": [
            {
                "id": candidate.id,
                "logical_model_id": candidate.logical_model_id,
                "provider_connection_id": provider_specs[candidate.provider]["id"],
                "upstream_model_id": candidate.provider_model_id,
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
                "pricing": candidate.pricing,
                "native_features": list(candidate.native_features),
                "unsupported_parameters": list(candidate.unsupported_parameters),
            }
            for model in catalog.values()
            for candidate in model.candidates
        ],
    }


def _runtime_provider_name(protocol: str) -> str:
    return "openai" if protocol == "openai-compatible" else protocol
