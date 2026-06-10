from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mixapi.errors import not_found


@dataclass(frozen=True)
class RouteDecisionRecord:
    request_id: str
    tenant_id: str
    project_id: str
    endpoint: str
    logical_model: str
    status: str
    selected_provider: str | None
    selected_provider_model: str | None
    attempts: tuple[dict[str, str], ...]
    rejected_candidates: tuple[dict[str, str], ...]
    selected_provider_connection_id: str | None = None
    selected_provider_protocol: str | None = None
    configuration_version: int = 0

    def public_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "endpoint": self.endpoint,
            "logical_model": self.logical_model,
            "status": self.status,
            "configuration_version": self.configuration_version,
            "selected_provider": self.selected_provider,
            "selected_provider_connection_id": self.selected_provider_connection_id,
            "selected_provider_protocol": self.selected_provider_protocol,
            "selected_provider_model": self.selected_provider_model,
            "attempts": list(self.attempts),
            "rejected_candidates": list(self.rejected_candidates),
            "fallback_used": len(self.attempts) > 1,
        }


@dataclass
class InMemoryRouteDecisionStore:
    _records: dict[tuple[str, str], RouteDecisionRecord] = field(default_factory=dict)

    def record(self, record: RouteDecisionRecord) -> None:
        self._records[(record.tenant_id, record.request_id)] = record

    def get_public(self, request_id: str, tenant_id: str) -> dict[str, Any]:
        record = self._records.get((tenant_id, request_id))
        if record is None:
            raise not_found("route_decision_not_found", "Route decision was not found.")
        return record.public_dict()
