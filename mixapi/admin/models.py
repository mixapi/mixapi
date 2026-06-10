from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Literal

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mixapi.auth import AdminPrincipal
from mixapi.errors import conflict, not_found, validation_error
from mixapi.repositories.configuration import (
    ConfigurationConflict,
    ConfigurationNotFound,
    PostgresConfigurationRepository,
)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelCreate(_Request):
    id: str = Field(min_length=1, max_length=200)
    description: str = ""
    aliases: tuple[str, ...] = ()
    status: Literal["active", "disabled"] = "disabled"


class ModelPatch(_Request):
    expected_updated_at: datetime
    description: str | None = None
    aliases: tuple[str, ...] | None = None
    status: Literal["active", "disabled"] | None = None


class CandidateCreate(_Request):
    logical_model_id: str
    provider_connection_id: str
    upstream_model_id: str
    status: Literal["active", "disabled"] = "active"
    priority: int = 100
    weight: int = Field(default=1, gt=0)
    context_window_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(ge=0)
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    tool_modes: tuple[str, ...] = ()
    schema_support: Literal[
        "none", "json_mode", "best_effort_schema", "strict_json_schema"
    ] = "none"
    streaming_support: bool = False
    embeddings_support: bool = False
    retention_class: str = "standard"
    regions: tuple[str, ...] = ()
    pricing: dict[str, Any] = Field(default_factory=dict)
    native_features: tuple[str, ...] = ()
    unsupported_parameters: tuple[str, ...] = ()


class CandidatePatch(_Request):
    expected_updated_at: datetime
    logical_model_id: str | None = None
    provider_connection_id: str | None = None
    upstream_model_id: str | None = None
    status: Literal["active", "disabled"] | None = None
    priority: int | None = None
    weight: int | None = Field(default=None, gt=0)
    context_window_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    input_modalities: tuple[str, ...] | None = None
    output_modalities: tuple[str, ...] | None = None
    tool_modes: tuple[str, ...] | None = None
    schema_support: Literal[
        "none", "json_mode", "best_effort_schema", "strict_json_schema"
    ] | None = None
    streaming_support: bool | None = None
    embeddings_support: bool | None = None
    retention_class: str | None = None
    regions: tuple[str, ...] | None = None
    pricing: dict[str, Any] | None = None
    native_features: tuple[str, ...] | None = None
    unsupported_parameters: tuple[str, ...] | None = None


def create_model_router(
    repository: PostgresConfigurationRepository,
    authenticate_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.post("/admin/v1/models", status_code=202)
    def create_model(body: dict[str, Any] = Body(default_factory=dict), admin=Depends(authenticate_admin)):
        request = _parse(ModelCreate, body)
        if request.status == "active":
            raise _candidate_required()
        try:
            mutation = repository.create_logical_model(
                model_id=request.id,
                description=request.description,
                aliases=request.aliases,
                status=request.status,
                actor_id=admin.actor_id,
            )
        except ConfigurationConflict as error:
            raise conflict("model_conflict", "Logical model or alias already exists.") from error
        except ValueError as error:
            raise validation_error("invalid_model_payload", "Logical model configuration is invalid.") from error
        return _response("logical_model", mutation)

    @router.get("/admin/v1/models")
    def list_models(_admin=Depends(authenticate_admin)):
        return {"object": "list", "data": [item.public_dict() for item in repository.list_logical_models()]}

    @router.get("/admin/v1/models/{model_id}")
    def get_model(model_id: str, _admin=Depends(authenticate_admin)):
        return _model(repository, model_id).public_dict()

    @router.patch("/admin/v1/models/{model_id}", status_code=202)
    def update_model(model_id: str, body: dict[str, Any] = Body(default_factory=dict), admin=Depends(authenticate_admin)):
        request = _parse(ModelPatch, body)
        changes = request.model_dump(exclude={"expected_updated_at"}, exclude_none=True)
        if not changes:
            raise validation_error("invalid_model_payload", "Logical model patch must include a change.")
        if changes.get("status") == "active" and repository.count_active_candidates(model_id) == 0:
            raise _candidate_required()
        try:
            mutation = repository.update_logical_model(
                model_id,
                actor_id=admin.actor_id,
                expected_updated_at=request.expected_updated_at,
                **changes,
            )
        except ConfigurationNotFound as error:
            raise not_found("model_not_found", "Logical model was not found.") from error
        except ConfigurationConflict as error:
            raise conflict("configuration_conflict", "Logical model was modified or its aliases conflict.") from error
        return _response("logical_model", mutation)

    @router.delete("/admin/v1/models/{model_id}", status_code=202)
    def delete_model(model_id: str, admin=Depends(authenticate_admin)):
        try:
            mutation = repository.soft_delete_logical_model(model_id, actor_id=admin.actor_id)
        except ConfigurationNotFound as error:
            raise not_found("model_not_found", "Logical model was not found.") from error
        return _response("logical_model", mutation)

    @router.post("/admin/v1/candidates", status_code=202)
    def create_candidate(body: dict[str, Any] = Body(default_factory=dict), admin=Depends(authenticate_admin)):
        request = _parse(CandidateCreate, body)
        _references(
            repository,
            request.logical_model_id,
            request.provider_connection_id,
            require_active=request.status == "active",
        )
        try:
            mutation = repository.create_candidate(**request.model_dump(), actor_id=admin.actor_id)
        except (ConfigurationConflict, ValueError) as error:
            raise validation_error("invalid_candidate_payload", "Model candidate configuration is invalid.") from error
        return _response("candidate", mutation)

    @router.get("/admin/v1/candidates")
    def list_candidates(logical_model_id: str | None = Query(default=None), _admin=Depends(authenticate_admin)):
        values = repository.list_candidates()
        if logical_model_id is not None:
            values = [item for item in values if item.logical_model_id == logical_model_id]
        return {"object": "list", "data": [item.public_dict() for item in values]}

    @router.get("/admin/v1/candidates/{candidate_id}")
    def get_candidate(candidate_id: str, _admin=Depends(authenticate_admin)):
        return _candidate(repository, candidate_id).public_dict()

    @router.patch("/admin/v1/candidates/{candidate_id}", status_code=202)
    def update_candidate(candidate_id: str, body: dict[str, Any] = Body(default_factory=dict), admin=Depends(authenticate_admin)):
        request = _parse(CandidatePatch, body)
        changes = request.model_dump(exclude={"expected_updated_at"}, exclude_none=True)
        if not changes:
            raise validation_error("invalid_candidate_payload", "Candidate patch must include a change.")
        current = _candidate(repository, candidate_id)
        next_model = changes.get("logical_model_id", current.logical_model_id)
        next_provider = changes.get("provider_connection_id", current.provider_connection_id)
        next_status = changes.get("status", current.status)
        _references(
            repository,
            next_model,
            next_provider,
            require_active=next_status == "active",
        )
        if current.status == "active" and (
            changes.get("status") == "disabled" or next_model != current.logical_model_id
        ):
            _protect_last(repository, current.logical_model_id, candidate_id)
        try:
            mutation = repository.update_candidate(
                candidate_id,
                actor_id=admin.actor_id,
                expected_updated_at=request.expected_updated_at,
                **changes,
            )
        except ConfigurationConflict as error:
            raise conflict("configuration_conflict", "Candidate was modified or conflicts.") from error
        except ValueError as error:
            raise validation_error("invalid_candidate_payload", "Model candidate configuration is invalid.") from error
        return _response("candidate", mutation)

    @router.delete("/admin/v1/candidates/{candidate_id}", status_code=202)
    def delete_candidate(candidate_id: str, admin=Depends(authenticate_admin)):
        current = _candidate(repository, candidate_id)
        if current.status == "active":
            _protect_last(repository, current.logical_model_id, candidate_id)
        mutation = repository.soft_delete_candidate(candidate_id, actor_id=admin.actor_id)
        return _response("candidate", mutation)

    return router


def _references(
    repository,
    model_id: str,
    provider_id: str,
    *,
    require_active: bool,
) -> None:
    try:
        model = repository.get_logical_model(model_id)
        provider = repository.get_provider(provider_id)
    except ConfigurationNotFound as error:
        raise validation_error("invalid_candidate_reference", "Candidate references an unknown model or provider.") from error
    if require_active and (model.status != "active" and model.status != "disabled"):
        raise validation_error("invalid_candidate_reference", "Candidate references an unavailable logical model.")
    if require_active and provider.status != "active":
        raise validation_error("invalid_candidate_reference", "Active candidate requires an active provider.")


def _protect_last(repository, model_id: str, candidate_id: str) -> None:
    model = _model(repository, model_id)
    if model.status == "active" and repository.count_active_candidates(
        model_id, exclude_candidate_id=candidate_id
    ) == 0:
        raise _candidate_required()


def _candidate_required():
    return validation_error("active_model_requires_candidate", "An active logical model requires an active candidate.")


def _model(repository, model_id):
    try:
        return repository.get_logical_model(model_id)
    except ConfigurationNotFound as error:
        raise not_found("model_not_found", "Logical model was not found.") from error


def _candidate(repository, candidate_id):
    try:
        return repository.get_candidate(candidate_id)
    except ConfigurationNotFound as error:
        raise not_found("candidate_not_found", "Model candidate was not found.") from error


def _parse(model, body):
    try:
        return model.model_validate(body)
    except ValidationError as error:
        raise validation_error("invalid_model_payload", "Model configuration payload is invalid.") from error


def _response(key, mutation):
    return {
        key: mutation.resource.public_dict(),
        "configuration_version": {
            "version": mutation.version.version,
            "status": mutation.version.status,
            "created_at": mutation.version.created_at.isoformat(),
        },
    }
