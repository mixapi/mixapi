from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


SchemaSupport = Literal["none", "json_mode", "best_effort_schema", "strict_json_schema"]
ProviderStatus = Literal["active", "disabled", "deprecated", "expired"]


@dataclass(frozen=True)
class ProviderModel:
    id: str
    provider: str
    provider_model_id: str
    logical_model_id: str
    status: ProviderStatus
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
    pricing: dict[str, Any] = field(default_factory=dict)
    native_features: tuple[str, ...] = ()
    unsupported_parameters: tuple[str, ...] = ()
    provider_connection_id: str | None = None
    priority: int = 100
    weight: int = 1


@dataclass(frozen=True)
class LogicalModel:
    id: str
    status: ProviderStatus
    description: str
    candidates: tuple[ProviderModel, ...]

    def public_dict(self) -> dict[str, Any]:
        active_candidates = [candidate for candidate in self.candidates if candidate.status == "active"]
        providers = sorted({candidate.provider for candidate in active_candidates})
        input_modalities = sorted(
            {modality for candidate in active_candidates for modality in candidate.input_modalities}
        )
        output_modalities = sorted(
            {modality for candidate in active_candidates for modality in candidate.output_modalities}
        )
        tool_modes = sorted({mode for candidate in active_candidates for mode in candidate.tool_modes})
        schema_support = _strongest_schema_support(
            [candidate.schema_support for candidate in active_candidates]
        )

        return {
            "id": self.id,
            "object": "model",
            "status": self.status,
            "description": self.description,
            "context_window_tokens": max(
                (candidate.context_window_tokens for candidate in active_candidates),
                default=0,
            ),
            "input_modalities": input_modalities,
            "output_modalities": output_modalities,
            "tool_modes": tool_modes,
            "schema_support": schema_support,
            "streaming": any(candidate.streaming_support for candidate in active_candidates),
            "embeddings": any(candidate.embeddings_support for candidate in active_candidates),
            "providers": providers,
            "pricing": {
                "currency": "usd",
                "unit": "tokens",
            },
        }


def _strongest_schema_support(values: list[SchemaSupport]) -> SchemaSupport:
    rank: dict[SchemaSupport, int] = {
        "none": 0,
        "json_mode": 1,
        "best_effort_schema": 2,
        "strict_json_schema": 3,
    }
    if not values:
        return "none"
    return max(values, key=lambda value: rank[value])
