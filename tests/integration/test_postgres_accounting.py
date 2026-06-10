from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import mixapi.app as app_module
from mixapi.auth import Principal
from tests.app_factory import create_app
from mixapi.postgres import PostgresPool
from mixapi.repositories.budgets import PostgresBudgetService
from mixapi.repositories.route_decisions import PostgresRouteDecisionStore
from mixapi.repositories.usage import PostgresUsageLedger
from mixapi.route_decisions import RouteDecisionRecord
from mixapi.settings import Settings
from mixapi.usage import UsageEvent, UsageQuery, UsageWriteIntent


MASTER_KEY = bytes.fromhex("66" * 32)


@pytest.fixture
def postgres_accounting(database_url: str, redis_url: str):
    settings = Settings(
        database_url=database_url,
        redis_url=redis_url,
        master_key=MASTER_KEY,
        master_key_version=1,
        previous_master_keys={},
    )
    pool = PostgresPool(settings)
    pool.open()
    with pool.connection() as connection:
        connection.execute(
            """
            TRUNCATE usage_events, route_decisions, budget_spend,
                     usage_write_intents, budget_reconciliation_outbox
            RESTART IDENTITY CASCADE
            """
        )
    try:
        yield (
            PostgresUsageLedger(pool),
            PostgresRouteDecisionStore(pool),
            PostgresBudgetService(pool),
            pool,
        )
    finally:
        pool.close()


def _event(
    request_id: str,
    created_at: datetime,
    *,
    tenant_id: str = "tenant_a",
    cost: str = "0.01000000",
) -> UsageEvent:
    return UsageEvent(
        request_id=request_id,
        tenant_id=tenant_id,
        project_id="project_a",
        api_key_id="key_a",
        endpoint="responses",
        logical_model="gpt-5.5",
        provider="gemini",
        provider_model="gemini-upstream",
        input_tokens=10,
        output_tokens=5,
        cost_usd=Decimal(cost),
        created_at=created_at,
        provider_connection_id="provider_a",
        configuration_version=3,
    )


def test_usage_queries_are_tenant_scoped_inclusive_and_keyset_paginated(
    postgres_accounting,
) -> None:
    usage, _routes, _budgets, _pool = postgres_accounting
    start = datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc)
    for event in (
        _event("before", start - timedelta(microseconds=1)),
        _event("start", start),
        _event("same-time", start),
        _event("end", start + timedelta(hours=1)),
        _event("other", start, tenant_id="tenant_b"),
    ):
        usage.record(event)

    first = usage.query(
        UsageQuery(
            tenant_id="tenant_a",
            start_time=start,
            end_time=start + timedelta(hours=1),
            limit=2,
        )
    )
    second = usage.query(
        UsageQuery(
            tenant_id="tenant_a",
            start_time=start,
            end_time=start + timedelta(hours=1),
            limit=2,
            after_created_at=first.events[-1].created_at,
            after_sequence_id=first.events[-1].sequence_id or 0,
        )
    )

    assert [event.request_id for event in first.events] == ["start", "same-time"]
    assert first.has_more is True
    assert [event.request_id for event in second.events] == ["end"]
    assert second.has_more is False


def test_route_decisions_are_upserted_and_tenant_scoped(postgres_accounting) -> None:
    _usage, routes, _budgets, _pool = postgres_accounting
    record = RouteDecisionRecord(
        request_id="req_route",
        tenant_id="tenant_a",
        project_id="project_a",
        endpoint="responses",
        logical_model="gpt-5.5",
        status="succeeded",
        selected_provider="gemini",
        selected_provider_model="gemini-upstream",
        attempts=({"provider": "gemini", "status": "succeeded"},),
        rejected_candidates=(),
        selected_provider_connection_id="provider_a",
        configuration_version=3,
    )

    routes.record(record)
    routes.record(record)

    assert routes.get_public("req_route", "tenant_a")["selected_provider"] == "gemini"
    with pytest.raises(Exception) as captured:
        routes.get_public("req_route", "tenant_b")
    assert getattr(captured.value, "code", None) == "route_decision_not_found"


def test_concurrent_settled_spend_increments_are_lossless(postgres_accounting) -> None:
    _usage, _routes, budgets, _pool = postgres_accounting

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda _index: budgets.add_settled_spend("key_a", Decimal("0.01")),
                range(40),
            )
        )

    assert budgets.actual_spend("key_a") == Decimal("0.40000000")


def test_usage_spend_outbox_and_intent_complete_atomically(postgres_accounting) -> None:
    usage, _routes, budgets, pool = postgres_accounting
    principal = Principal(
        tenant_id="tenant_a",
        project_id="project_a",
        api_key_id="key_a",
        scopes=("responses:create",),
    )
    reservation = budgets.reserve(principal, Decimal("0.02"), limit_usd=Decimal("1.00"))
    usage.begin_intent(
        UsageWriteIntent(
            request_id="req_atomic",
            tenant_id="tenant_a",
            project_id="project_a",
            api_key_id="key_a",
            endpoint="responses",
            logical_model="gpt-5.5",
            configuration_version=3,
            reservation_data={"budget_reservation_id": reservation.reservation_id},
        )
    )

    budgets.reconcile_with_usage(
        reservation,
        Decimal("0.015"),
        usage,
        [_event("req_atomic", datetime.now(timezone.utc), cost="0.015")],
    )

    with pool.connection() as connection:
        counts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM usage_events) AS usage_count,
              (SELECT count(*) FROM budget_reconciliation_outbox) AS outbox_count,
              (SELECT status FROM usage_write_intents WHERE request_id = 'req_atomic') AS intent_status
            """
        ).fetchone()

    assert counts == {"usage_count": 1, "outbox_count": 1, "intent_status": "completed"}
    assert budgets.actual_spend("key_a") == Decimal("0.01500000")


def test_duplicate_reconciliation_is_exactly_once(postgres_accounting) -> None:
    usage, _routes, budgets, _pool = postgres_accounting
    reservation = budgets.reserve(
        Principal(
            tenant_id="tenant_a",
            project_id="project_a",
            api_key_id="key_a",
            scopes=("responses:create",),
        ),
        Decimal("0.02"),
        limit_usd=Decimal("1.00"),
    )
    event = _event("req_once", datetime.now(timezone.utc), cost="0.01")

    budgets.reconcile_with_usage(reservation, Decimal("0.01"), usage, [event])
    budgets.reconcile_with_usage(reservation, Decimal("0.01"), usage, [event])

    assert budgets.actual_spend("key_a") == Decimal("0.01000000")
    assert [item.request_id for item in usage.events()] == ["req_once"]


def test_intent_write_failure_prevents_provider_dispatch(
    database_url: str,
    redis_url: str,
    monkeypatch,
) -> None:
    settings = Settings(
        database_url=database_url,
        redis_url=redis_url,
        master_key=MASTER_KEY,
        master_key_version=1,
        previous_master_keys={},
    )
    app = create_app(settings=settings)
    dispatched = False

    def dispatch_never_runs(**_kwargs):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("provider dispatch must follow durable intent creation")

    monkeypatch.setattr(app_module, "_dispatch_response_with_fallback", dispatch_never_runs)

    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.postgres_pool.close()
        response = client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer dev-key"},
            json={"model": "mixapi/balanced-chat", "input": "do not dispatch"},
        )

    assert response.status_code == 503
    assert dispatched is False
