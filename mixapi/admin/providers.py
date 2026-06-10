from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Literal

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mixapi.auth import AdminPrincipal
from mixapi.errors import conflict, not_found, validation_error
from mixapi.provider_testing import ProviderConnectionTester
from mixapi.repositories.configuration import (
    ConfigurationConflict,
    ConfigurationNotFound,
    PostgresConfigurationRepository,
)


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderCreateRequest(_RequestModel):
    name: str = Field(min_length=1, max_length=200)
    protocol: Literal["openai-compatible", "anthropic", "gemini", "ollama"]
    base_url: str = Field(min_length=1, max_length=2048)
    credential: str
    timeout_seconds: Decimal = Field(default=Decimal("30"), gt=0)
    status: Literal["active", "disabled"] = "active"
    priority: int = 100
    weight: int = Field(default=1, gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderPatchRequest(_RequestModel):
    expected_updated_at: datetime
    name: str | None = Field(default=None, min_length=1, max_length=200)
    protocol: Literal["openai-compatible", "anthropic", "gemini", "ollama"] | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    credential: str | None = None
    timeout_seconds: Decimal | None = Field(default=None, gt=0)
    status: Literal["active", "disabled"] | None = None
    priority: int | None = None
    weight: int | None = Field(default=None, gt=0)
    metadata: dict[str, Any] | None = None


def create_provider_router(
    repository: PostgresConfigurationRepository,
    authenticate_admin: Callable[..., AdminPrincipal],
    tester: ProviderConnectionTester,
) -> APIRouter:
    router = APIRouter(prefix="/admin/v1/providers")

    @router.post("", status_code=202)
    def create_provider(
        request_body: dict[str, Any] = Body(default_factory=dict),
        admin: AdminPrincipal = Depends(authenticate_admin),
    ) -> dict[str, Any]:
        request = _parse(ProviderCreateRequest, request_body)
        try:
            mutation = repository.create_provider(
                **request.model_dump(),
                actor_id=admin.actor_id,
            )
        except ConfigurationConflict as error:
            raise conflict("provider_conflict", "Provider name already exists.") from error
        except ValueError as error:
            raise validation_error(
                "invalid_provider_payload",
                "Provider connection configuration is invalid.",
            ) from error
        return _mutation_response(mutation)

    @router.get("")
    def list_providers(
        _admin: AdminPrincipal = Depends(authenticate_admin),
    ) -> dict[str, Any]:
        return {
            "object": "list",
            "data": [provider.public_dict() for provider in repository.list_providers()],
        }

    @router.get("/{provider_id}")
    def get_provider(
        provider_id: str,
        _admin: AdminPrincipal = Depends(authenticate_admin),
    ) -> dict[str, Any]:
        return _get(repository, provider_id).public_dict()

    @router.patch("/{provider_id}", status_code=202)
    def update_provider(
        provider_id: str,
        request_body: dict[str, Any] = Body(default_factory=dict),
        admin: AdminPrincipal = Depends(authenticate_admin),
    ) -> dict[str, Any]:
        request = _parse(ProviderPatchRequest, request_body)
        changes = request.model_dump(exclude={"expected_updated_at", "credential"}, exclude_none=True)
        if not changes and request.credential is None:
            raise validation_error(
                "invalid_provider_payload",
                "Provider patch must include at least one change.",
            )
        try:
            if changes:
                mutation = repository.update_provider(
                    provider_id,
                    actor_id=admin.actor_id,
                    expected_updated_at=request.expected_updated_at,
                    **changes,
                )
                expected_updated_at = mutation.resource.updated_at
            else:
                expected_updated_at = request.expected_updated_at
                mutation = None
            if request.credential is not None:
                mutation = repository.rotate_provider_credential(
                    provider_id,
                    request.credential,
                    actor_id=admin.actor_id,
                    expected_updated_at=expected_updated_at,
                )
            assert mutation is not None
        except ConfigurationNotFound as error:
            raise not_found("provider_not_found", "Provider connection was not found.") from error
        except ConfigurationConflict as error:
            raise conflict(
                "configuration_conflict",
                "Provider connection was modified by another request.",
            ) from error
        except ValueError as error:
            raise validation_error(
                "invalid_provider_payload",
                "Provider connection configuration is invalid.",
            ) from error
        return _mutation_response(mutation)

    @router.delete("/{provider_id}", status_code=202)
    def delete_provider(
        provider_id: str,
        admin: AdminPrincipal = Depends(authenticate_admin),
    ) -> dict[str, Any]:
        try:
            mutation = repository.soft_delete_provider(provider_id, actor_id=admin.actor_id)
        except ConfigurationNotFound as error:
            raise not_found("provider_not_found", "Provider connection was not found.") from error
        return _mutation_response(mutation)

    @router.post("/{provider_id}/test")
    def test_provider(
        provider_id: str,
        _admin: AdminPrincipal = Depends(authenticate_admin),
    ) -> dict[str, object]:
        return tester.test(_get(repository, provider_id)).public_dict()

    return router


def _get(repository: PostgresConfigurationRepository, provider_id: str):
    try:
        return repository.get_provider(provider_id)
    except ConfigurationNotFound as error:
        raise not_found("provider_not_found", "Provider connection was not found.") from error


def _parse(model: type[BaseModel], payload: dict[str, Any]):
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise validation_error(
            "invalid_provider_payload",
            "Provider connection payload is invalid.",
        ) from error


def _mutation_response(mutation) -> dict[str, Any]:
    return {
        "provider": mutation.resource.public_dict(),
        "configuration_version": {
            "version": mutation.version.version,
            "status": mutation.version.status,
            "created_at": mutation.version.created_at.isoformat(),
        },
    }
