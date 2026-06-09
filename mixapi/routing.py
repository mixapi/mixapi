from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from mixapi.errors import capability_unsupported, validation_error
from mixapi.models import LogicalModel, ProviderModel, SchemaSupport


@dataclass(frozen=True)
class RouteDecision:
    candidate: ProviderModel
    candidates: tuple[ProviderModel, ...]
    rejected: tuple[dict[str, str], ...]
    score: Decimal


def plan_route(
    catalog: dict[str, LogicalModel],
    request_body: dict[str, Any],
    endpoint: str,
) -> RouteDecision:
    logical_model_id = request_body.get("model")
    if not isinstance(logical_model_id, str) or not logical_model_id:
        raise validation_error("model_required", "Request field `model` is required.")

    logical_model = catalog.get(logical_model_id)
    if logical_model is None:
        raise capability_unsupported("model_not_found", "No logical model matches the request.")

    required_input_modalities = _required_input_modalities(request_body)
    required_output_modality = "embedding" if endpoint == "embeddings" else "text"
    requires_strict_schema = _requires_strict_schema(request_body)
    requires_function_tools = _requires_function_tools(request_body)
    provider_pin = _provider_pin(request_body)
    objective = _routing_objective(request_body)

    eligible: list[tuple[ProviderModel, Decimal]] = []
    rejected: list[dict[str, str]] = []

    for candidate in logical_model.candidates:
        rejection = _rejection_reason(
            candidate=candidate,
            provider_pin=provider_pin,
            required_input_modalities=required_input_modalities,
            required_output_modality=required_output_modality,
            requires_strict_schema=requires_strict_schema,
            requires_function_tools=requires_function_tools,
            endpoint=endpoint,
        )
        if rejection:
            rejected.append({"provider": candidate.provider, "model": candidate.provider_model_id, "reason": rejection})
            continue

        eligible.append((candidate, _score_candidate(candidate, objective)))

    if not eligible:
        code = _best_rejection_code(rejected)
        raise capability_unsupported(code, "No eligible provider supports the requested features.")

    ordered = sorted(eligible, key=lambda item: (item[1], item[0].provider))
    candidate, score = ordered[0]
    return RouteDecision(
        candidate=candidate,
        candidates=tuple(item[0] for item in ordered),
        rejected=tuple(rejected),
        score=score,
    )


def _rejection_reason(
    candidate: ProviderModel,
    provider_pin: str | None,
    required_input_modalities: set[str],
    required_output_modality: str,
    requires_strict_schema: bool,
    requires_function_tools: bool,
    endpoint: str,
) -> str | None:
    if candidate.status != "active":
        return "model_disabled"
    if provider_pin and candidate.provider != provider_pin:
        return "provider_not_pinned"
    if endpoint == "embeddings" and not candidate.embeddings_support:
        return "embeddings_not_supported"
    if required_output_modality not in candidate.output_modalities:
        return "missing_output_modality"
    if missing := required_input_modalities.difference(candidate.input_modalities):
        if "image" in missing:
            return "missing_input_modality"
        return "missing_input_modality"
    if requires_strict_schema and not _schema_supports(candidate.schema_support, "strict_json_schema"):
        return "strict_schema_not_supported"
    if requires_function_tools and "function" not in candidate.tool_modes:
        return "tools_not_supported"
    return None


def _required_input_modalities(request_body: dict[str, Any]) -> set[str]:
    modalities: set[str] = set()

    raw_input = request_body.get("input", [])
    if isinstance(raw_input, str):
        return {"text"}
    if not isinstance(raw_input, list):
        raise validation_error("invalid_input", "Request field `input` must be a string or list.")

    for message in raw_input:
        if not isinstance(message, dict):
            continue
        content = message.get("content", [])
        if isinstance(content, str):
            modalities.add("text")
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in {"input_text", "text"}:
                modalities.add("text")
            elif part_type in {"input_image", "image"}:
                modalities.add("image")
            elif part_type in {"input_file", "file"}:
                modalities.add("file")

    return modalities or {"text"}


def _requires_strict_schema(request_body: dict[str, Any]) -> bool:
    response_config = request_body.get("response")
    if not isinstance(response_config, dict):
        return False
    response_format = response_config.get("format")
    if not isinstance(response_format, dict):
        return False
    return response_format.get("type") == "json_schema"


def _requires_function_tools(request_body: dict[str, Any]) -> bool:
    tools = request_body.get("tools")
    return isinstance(tools, list) and bool(tools)


def _provider_pin(request_body: dict[str, Any]) -> str | None:
    native = request_body.get("native")
    if not isinstance(native, dict):
        return None
    provider = native.get("provider")
    if provider in {None, "auto"}:
        return None
    if not isinstance(provider, str):
        raise validation_error("invalid_provider_pin", "`native.provider` must be a string.")
    return provider


def _routing_objective(request_body: dict[str, Any]) -> str:
    routing = request_body.get("routing")
    if not isinstance(routing, dict):
        return "balanced"
    objective = routing.get("objective", "balanced")
    if objective not in {"balanced", "lowest-cost", "lowest-latency", "highest-reliability"}:
        raise validation_error("invalid_routing_objective", "Unsupported routing objective.")
    return objective


def _score_candidate(candidate: ProviderModel, objective: str) -> Decimal:
    cost = _token_cost(candidate)
    if objective == "lowest-cost":
        return cost
    if objective == "lowest-latency" and candidate.provider == "ollama":
        return Decimal("100")
    if objective == "highest-reliability" and candidate.provider == "ollama":
        return Decimal("10")
    preference = {
        "openai": Decimal("0.01"),
        "anthropic": Decimal("0.02"),
        "gemini": Decimal("0.03"),
        "ollama": Decimal("0.04"),
    }.get(candidate.provider, Decimal("1"))
    return cost + preference


def _token_cost(candidate: ProviderModel) -> Decimal:
    input_cost = Decimal(str(candidate.pricing.get("input_per_million", "0")))
    output_cost = Decimal(str(candidate.pricing.get("output_per_million", "0")))
    return input_cost + output_cost


def _best_rejection_code(rejected: list[dict[str, str]]) -> str:
    for rejection in rejected:
        if rejection["reason"] != "provider_not_pinned":
            return rejection["reason"]
    return rejected[0]["reason"] if rejected else "no_eligible_provider"


def _schema_supports(actual: SchemaSupport, required: SchemaSupport) -> bool:
    rank: dict[SchemaSupport, int] = {
        "none": 0,
        "json_mode": 1,
        "best_effort_schema": 2,
        "strict_json_schema": 3,
    }
    return rank[actual] >= rank[required]
