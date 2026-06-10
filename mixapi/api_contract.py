from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import models_json_schema


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FlexibleContractModel(ContractModel):
    model_config = ConfigDict(extra="allow")


class RoutingOptions(ContractModel):
    objective: Literal[
        "balanced",
        "lowest-cost",
        "lowest-latency",
        "highest-reliability",
    ] | None = None
    max_cost_usd: str | int | float | None = None


class NativeOptions(ContractModel):
    provider: str | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)


class InputContentPart(FlexibleContractModel):
    type: Literal["input_text", "text", "input_image", "image", "input_file", "file"]
    text: str | None = None
    image_url: str | None = None
    file_ref: str | None = None
    file_url: str | None = None


class InputMessage(FlexibleContractModel):
    role: str
    content: str | list[InputContentPart]


class FunctionTool(ContractModel):
    type: Literal["function"]
    name: str
    description: str | None = None
    parameters: dict[str, Any]
    strict: bool | None = None


class ResponseFormat(ContractModel):
    type: Literal["text", "json_object", "json_schema"]
    json_schema: dict[str, Any] | None = None


class ResponseConfiguration(ContractModel):
    format: ResponseFormat


class ResponseCreateRequest(FlexibleContractModel):
    model: str
    input: str | list[InputMessage]
    stream: bool = False
    max_output_tokens: int | None = Field(default=None, gt=0)
    tools: list[FunctionTool] | None = None
    tool_choice: Any | None = None
    response: ResponseConfiguration | None = None
    routing: RoutingOptions | None = None
    native: NativeOptions | None = None


class EmbeddingCreateRequest(FlexibleContractModel):
    model: str
    input: str | list[str]
    routing: RoutingOptions | None = None
    native: NativeOptions | None = None


class BillableUnit(ContractModel):
    type: str
    quantity: int = Field(ge=0)


class TokenUsage(ContractModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    billable_units: list[BillableUnit]


class Cost(ContractModel):
    provider_cost_usd: str
    platform_cost_usd: str


class FailedAttempt(ContractModel):
    provider: str
    provider_model: str
    status: Literal["failed"]
    reason: str


class RouteAttempt(ContractModel):
    provider: str
    provider_model: str
    status: Literal["failed", "succeeded"]
    reason: str | None = None


class RejectedCandidate(ContractModel):
    provider: str
    model: str
    reason: str


class RouteMetadata(ContractModel):
    attempts: int = Field(ge=1)
    fallback_used: bool
    selected_provider_model: str
    failed_attempts: list[FailedAttempt]
    decision_trace_id: str
    rejected_candidates: list[RejectedCandidate]


class ResponseOutputItem(FlexibleContractModel):
    type: str
    role: str | None = None
    content: list[dict[str, Any]] | None = None
    name: str | None = None
    arguments: Any | None = None
    call_id: str | None = None


class ResponseObject(ContractModel):
    id: str
    object: Literal["response"]
    model: str
    provider: str
    output: list[ResponseOutputItem]
    output_text: str
    usage: TokenUsage
    cost: Cost
    route: RouteMetadata


class EmbeddingItem(ContractModel):
    object: Literal["embedding"]
    index: int = Field(ge=0)
    embedding: list[float]


class EmbeddingList(ContractModel):
    object: Literal["list"]
    model: str
    provider: str
    data: list[EmbeddingItem]
    usage: TokenUsage
    cost: Cost
    route: RouteMetadata


class ModelPricing(ContractModel):
    currency: Literal["usd"]
    unit: Literal["tokens"]


class ModelCapability(ContractModel):
    id: str
    object: Literal["model"]
    status: str
    description: str
    context_window_tokens: int = Field(ge=0)
    input_modalities: list[str]
    output_modalities: list[str]
    tool_modes: list[str]
    schema_support: str
    streaming: bool
    embeddings: bool
    providers: list[str]
    pricing: ModelPricing


class ModelList(ContractModel):
    object: Literal["list"]
    data: list[ModelCapability]


class RouteDecision(ContractModel):
    request_id: str
    tenant_id: str
    project_id: str
    endpoint: str
    logical_model: str
    status: str
    selected_provider: str | None
    selected_provider_model: str | None
    attempts: list[RouteAttempt]
    rejected_candidates: list[RejectedCandidate]
    fallback_used: bool


class UsageEvent(ContractModel):
    request_id: str
    tenant_id: str
    project_id: str
    api_key_id: str
    endpoint: str
    logical_model: str
    provider: str
    provider_model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: str
    created_at: datetime


class UsageSummary(ContractModel):
    request_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: str


class UsageList(ContractModel):
    object: Literal["list"]
    data: list[UsageEvent]
    has_more: bool
    next_cursor: str | None
    summary: UsageSummary


class ApiKeyCreateRequest(ContractModel):
    tenant_id: str
    project_id: str
    name: str | None = None
    scopes: list[str] = Field(
        default_factory=lambda: ["models:read", "responses:create", "embeddings:create"]
    )
    model_allowlist: list[str] = Field(default_factory=list)
    budget_limit_usd: str | int | float | None = None
    expires_at: datetime | None = None


class ApiKeyRecord(ContractModel):
    id: str
    tenant_id: str
    project_id: str
    name: str | None
    key_prefix: str
    scopes: list[str]
    model_allowlist: list[str]
    budget_limit_usd: str | None
    expires_at: datetime | None
    status: Literal["active", "revoked"]
    created_at: datetime
    updated_at: datetime


class ApiKeyCreated(ApiKeyRecord):
    secret: str


class ApiKeyList(ContractModel):
    object: Literal["list"]
    data: list[ApiKeyRecord]


class ApiKeyUpdateRequest(ContractModel):
    name: str | None = None
    scopes: list[str] | None = None
    model_allowlist: list[str] | None = None
    budget_limit_usd: str | int | float | None = None
    expires_at: datetime | None = None
    status: Literal["active", "revoked"] | None = None


class TenantModelAllowlistRequest(ContractModel):
    models: list[str]


class TenantRoutingPolicyRequest(ContractModel):
    objective: Literal[
        "balanced",
        "lowest-cost",
        "lowest-latency",
        "highest-reliability",
    ] | None


class TenantPolicy(ContractModel):
    tenant_id: str
    model_allowlist: list[str]
    routing_objective: str | None
    updated_at: datetime | None


class AuditEvent(ContractModel):
    id: str
    actor_id: str
    tenant_id: str
    action: str
    target_type: str
    target_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    created_at: datetime


class AuditEventList(ContractModel):
    object: Literal["list"]
    data: list[AuditEvent]


class ErrorDetail(ContractModel):
    type: str
    code: str
    message: str
    request_id: str
    trace_id: str


class ErrorEnvelope(ContractModel):
    error: ErrorDetail


class StreamErrorDetail(ContractModel):
    type: Literal["stream_interrupted"]
    code: Literal["provider_stream_interrupted"]
    message: str


class ResponseCreatedEvent(ContractModel):
    id: str
    model: str
    provider: str


class ResponseOutputTextDeltaEvent(ContractModel):
    delta: str


class ResponseCompletedEvent(ContractModel):
    response: ResponseObject


class ResponseErrorEvent(ContractModel):
    error: StreamErrorDetail


CONTRACT_MODEL_TYPES: tuple[type[ContractModel], ...] = (
    RoutingOptions,
    NativeOptions,
    InputContentPart,
    InputMessage,
    FunctionTool,
    ResponseFormat,
    ResponseConfiguration,
    ResponseCreateRequest,
    EmbeddingCreateRequest,
    BillableUnit,
    TokenUsage,
    Cost,
    FailedAttempt,
    RouteAttempt,
    RejectedCandidate,
    RouteMetadata,
    ResponseOutputItem,
    ResponseObject,
    EmbeddingItem,
    EmbeddingList,
    ModelPricing,
    ModelCapability,
    ModelList,
    RouteDecision,
    UsageEvent,
    UsageSummary,
    UsageList,
    ApiKeyCreateRequest,
    ApiKeyRecord,
    ApiKeyCreated,
    ApiKeyList,
    ApiKeyUpdateRequest,
    TenantModelAllowlistRequest,
    TenantRoutingPolicyRequest,
    TenantPolicy,
    AuditEvent,
    AuditEventList,
    ErrorDetail,
    ErrorEnvelope,
    StreamErrorDetail,
    ResponseCreatedEvent,
    ResponseOutputTextDeltaEvent,
    ResponseCompletedEvent,
    ResponseErrorEvent,
)
CONTRACT_MODELS = {model.__name__: model for model in CONTRACT_MODEL_TYPES}


@dataclass(frozen=True)
class OperationContract:
    operation_id: str
    response_model: str | None
    request_model: str | None = None
    success_status: str = "200"
    streaming: bool = False
    export: bool = False


OPERATION_CONTRACTS: dict[tuple[str, str], OperationContract] = {
    ("/v1/models", "get"): OperationContract("listModels", "ModelList"),
    ("/v1/responses", "post"): OperationContract(
        "createResponse",
        "ResponseObject",
        "ResponseCreateRequest",
        streaming=True,
    ),
    ("/v1/embeddings", "post"): OperationContract(
        "createEmbedding",
        "EmbeddingList",
        "EmbeddingCreateRequest",
    ),
    ("/v1/route-decisions/{request_id}", "get"): OperationContract(
        "getRouteDecision",
        "RouteDecision",
    ),
    ("/v1/usage", "get"): OperationContract("getUsage", "UsageList"),
    ("/v1/usage/export", "get"): OperationContract(
        "exportUsage",
        None,
        export=True,
    ),
    ("/admin/v1/api-keys", "post"): OperationContract(
        "createApiKey",
        "ApiKeyCreated",
        "ApiKeyCreateRequest",
        success_status="201",
    ),
    ("/admin/v1/api-keys", "get"): OperationContract("listApiKeys", "ApiKeyList"),
    ("/admin/v1/api-keys/{api_key_id}", "patch"): OperationContract(
        "updateApiKey",
        "ApiKeyRecord",
        "ApiKeyUpdateRequest",
    ),
    ("/admin/v1/api-keys/{api_key_id}", "delete"): OperationContract(
        "revokeApiKey",
        "ApiKeyRecord",
    ),
    ("/admin/v1/tenants/{tenant_id}/model-allowlist", "put"): OperationContract(
        "setTenantModelAllowlist",
        "TenantPolicy",
        "TenantModelAllowlistRequest",
    ),
    ("/admin/v1/tenants/{tenant_id}/routing-policy", "put"): OperationContract(
        "setTenantRoutingPolicy",
        "TenantPolicy",
        "TenantRoutingPolicyRequest",
    ),
    ("/admin/v1/audit-events", "get"): OperationContract(
        "listAuditEvents",
        "AuditEventList",
    ),
}


def build_openapi_contract(app: FastAPI) -> dict[str, Any]:
    if app.openapi_schema is not None:
        return app.openapi_schema

    contract = get_openapi(
        title=app.title,
        version=app.version,
        description="Unified API for routed model inference and gateway administration.",
        routes=app.routes,
    )
    contract["components"] = {
        **contract.get("components", {}),
        "schemas": _component_schemas(),
        "securitySchemes": {
            "ServiceBearerAuth": {"type": "http", "scheme": "bearer"},
            "AdminBearerAuth": {"type": "http", "scheme": "bearer"},
        },
    }
    for (path, method), operation_contract in OPERATION_CONTRACTS.items():
        operation = contract["paths"][path][method]
        operation["operationId"] = operation_contract.operation_id
        operation["tags"] = ["Admin" if path.startswith("/admin/") else "Public"]
        operation["security"] = [
            {"AdminBearerAuth" if path.startswith("/admin/") else "ServiceBearerAuth": []}
        ]
        operation["parameters"] = [
            parameter
            for parameter in operation.get("parameters", [])
            if parameter.get("name", "").lower() != "authorization"
        ]
        if not operation["parameters"]:
            operation.pop("parameters", None)
        if operation_contract.request_model is not None:
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _schema_ref(operation_contract.request_model)
                    }
                },
            }
        success_response = operation["responses"].setdefault(
            operation_contract.success_status,
            {"description": "Successful response"},
        )
        if operation_contract.response_model is not None:
            success_response["content"] = {
                "application/json": {
                    "schema": _schema_ref(operation_contract.response_model)
                }
            }
        if operation_contract.streaming:
            success_response.setdefault("content", {})["text/event-stream"] = {
                "schema": {
                    "oneOf": [
                        _schema_ref("ResponseCreatedEvent"),
                        _schema_ref("ResponseOutputTextDeltaEvent"),
                        _schema_ref("ResponseCompletedEvent"),
                        _schema_ref("ResponseErrorEvent"),
                    ]
                }
            }
        if operation_contract.export:
            success_response["content"] = {
                "text/csv": {"schema": {"type": "string"}},
                "application/x-ndjson": {"schema": {"type": "string"}},
            }
        operation["responses"].pop("422", None)
        for status in ("400", "401", "402", "403", "404", "429", "503", "504"):
            operation["responses"].setdefault(
                status,
                {
                    "description": "Normalized MixAPI error",
                    "content": {
                        "application/json": {"schema": _schema_ref("ErrorEnvelope")}
                    },
                },
            )

    app.openapi_schema = contract
    return contract


def install_openapi_contract(app: FastAPI) -> None:
    def custom_openapi() -> dict[str, Any]:
        return build_openapi_contract(app)

    app.openapi = custom_openapi


def _component_schemas() -> dict[str, Any]:
    _, schema = models_json_schema(
        [(model, "validation") for model in CONTRACT_MODEL_TYPES],
        ref_template="#/components/schemas/{model}",
    )
    return schema["$defs"]


def _schema_ref(model_name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{model_name}"}
