from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends

from mixapi.auth import AdminPrincipal
from mixapi.errors import conflict, not_found
from mixapi.publication import ConfigurationPublisher, RebuildInProgress
from mixapi.repositories.configuration import ConfigurationNotFound, PostgresConfigurationRepository
from mixapi.runtime.snapshots import RedisSnapshotStore


def create_publication_router(
    repository: PostgresConfigurationRepository,
    publisher: ConfigurationPublisher,
    snapshots: RedisSnapshotStore,
    authenticate_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/admin/v1/configuration")

    @router.get("/versions/{version}")
    def get_version(version: int, _admin=Depends(authenticate_admin)):
        try:
            record = repository.get_configuration_version(version)
        except ConfigurationNotFound as error:
            raise not_found("configuration_version_not_found", "Configuration version was not found.") from error
        return {
            "version": record.version,
            "status": record.status,
            "checksum": record.checksum,
            "error_code": record.error_code,
            "error_message": record.error_message,
            "created_at": record.created_at.isoformat(),
            "published_at": record.published_at.isoformat() if record.published_at else None,
            "failed_at": record.failed_at.isoformat() if record.failed_at else None,
        }

    @router.post("/rebuild", status_code=202)
    def rebuild(_admin=Depends(authenticate_admin)):
        try:
            changed = publisher.rebuild()
        except RebuildInProgress as error:
            raise conflict("rebuild_in_progress", "A configuration rebuild is already in progress.") from error
        return {
            "status": "rebuilt" if changed else "current",
            "active_version": snapshots.active_version(),
        }

    return router
