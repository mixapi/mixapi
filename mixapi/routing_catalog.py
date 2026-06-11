from __future__ import annotations

from decimal import Decimal
from typing import Any

from mixapi.models import LogicalModel, ProviderModel
from mixapi.snapshots import ConfigurationSnapshot


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
                provider=provider.name,
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
                provider_connection_name=provider.name,
                protocol=provider.protocol,
                priority=candidate.priority,
                weight=candidate.weight,
                latency_ms=_decimal_metadata(provider.metadata, "latency_ms", "1000"),
                reliability=_decimal_metadata(provider.metadata, "reliability", "0"),
                concurrency_limit=provider.metadata.get("concurrency_limit"),
                request_quota=provider.metadata.get("request_quota"),
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


def _decimal_metadata(metadata: dict[str, Any], key: str, default: str) -> Decimal:
    return Decimal(str(metadata.get(key, default)))
