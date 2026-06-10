from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from mixapi.errors import not_found
from mixapi.postgres import PostgresPool
from mixapi.route_decisions import RouteDecisionRecord


class PostgresRouteDecisionStore:
    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def record(self, record: RouteDecisionRecord) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO route_decisions (
                    tenant_id, request_id, project_id, endpoint, logical_model,
                    configuration_version, status,
                    selected_provider_connection_id, selected_provider_name,
                    selected_provider_protocol, selected_provider_model,
                    attempts, rejected_candidates
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, request_id) DO UPDATE SET
                    project_id = EXCLUDED.project_id,
                    endpoint = EXCLUDED.endpoint,
                    logical_model = EXCLUDED.logical_model,
                    configuration_version = EXCLUDED.configuration_version,
                    status = EXCLUDED.status,
                    selected_provider_connection_id = EXCLUDED.selected_provider_connection_id,
                    selected_provider_name = EXCLUDED.selected_provider_name,
                    selected_provider_protocol = EXCLUDED.selected_provider_protocol,
                    selected_provider_model = EXCLUDED.selected_provider_model,
                    attempts = EXCLUDED.attempts,
                    rejected_candidates = EXCLUDED.rejected_candidates
                """,
                (
                    record.tenant_id,
                    record.request_id,
                    record.project_id,
                    record.endpoint,
                    record.logical_model,
                    record.configuration_version,
                    record.status,
                    record.selected_provider_connection_id,
                    record.selected_provider,
                    record.selected_provider_protocol,
                    record.selected_provider_model,
                    Jsonb(list(record.attempts)),
                    Jsonb(list(record.rejected_candidates)),
                ),
            )

    def get_public(self, request_id: str, tenant_id: str) -> dict[str, Any]:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM route_decisions
                WHERE tenant_id = %s AND request_id = %s
                """,
                (tenant_id, request_id),
            ).fetchone()
        if row is None:
            raise not_found("route_decision_not_found", "Route decision was not found.")
        return RouteDecisionRecord(
            request_id=row["request_id"],
            tenant_id=row["tenant_id"],
            project_id=row["project_id"],
            endpoint=row["endpoint"],
            logical_model=row["logical_model"],
            status=row["status"],
            selected_provider=row["selected_provider_name"],
            selected_provider_model=row["selected_provider_model"],
            attempts=tuple(row["attempts"]),
            rejected_candidates=tuple(row["rejected_candidates"]),
            selected_provider_connection_id=row["selected_provider_connection_id"],
            selected_provider_protocol=row["selected_provider_protocol"],
            configuration_version=row["configuration_version"],
        ).public_dict()
