from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
import redis

from mixapi.auth import Principal
from mixapi.errors import MixAPIError
from mixapi.postgres import PostgresPool
from mixapi.repositories.budgets import PostgresBudgetService
from mixapi.runtime.budget import RedisBudgetService
from mixapi.runtime.circuits import RedisCircuitBreaker
from mixapi.runtime.idempotency import RedisIdempotencyStore
from mixapi.runtime.quota import RedisQuotaService
from mixapi.settings import Settings


MASTER_KEY = bytes.fromhex("77" * 32)


@pytest.fixture
def redis_runtime_services(database_url: str, redis_url: str):
    client_a = redis.Redis.from_url(redis_url, decode_responses=True)
    client_b = redis.Redis.from_url(redis_url, decode_responses=True)
    client_a.flushdb()
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
            TRUNCATE usage_events, budget_spend, usage_write_intents,
                     budget_reconciliation_outbox RESTART IDENTITY CASCADE
            """
        )
    try:
        yield client_a, client_b, PostgresBudgetService(pool), pool
    finally:
        client_a.flushdb()
        client_a.close()
        client_b.close()
        pool.close()


def _principal() -> Principal:
    return Principal(
        tenant_id="tenant_a",
        project_id="project_a",
        api_key_id="key_a",
        scopes=("responses:create",),
    )


def test_idempotency_is_first_write_wins_and_body_hash_bound(redis_runtime_services) -> None:
    client_a, client_b, _durable, _pool = redis_runtime_services
    stores = (
        RedisIdempotencyStore(client_a, namespace="test", ttl_seconds=60),
        RedisIdempotencyStore(client_b, namespace="test", ttl_seconds=60),
    )
    body = {"model": "gpt-5.5", "input": "hello"}

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(
            executor.map(
                lambda pair: pair[0].store(
                    _principal(), "responses", "idem", body, pair[1]
                ),
                ((stores[0], {"id": "first"}), (stores[1], {"id": "second"})),
            )
        )

    replay = stores[0].replay(_principal(), "responses", "idem", body)
    assert replay is not None
    assert replay.response in ({"id": "first"}, {"id": "second"})
    idempotency_key = client_a.keys("test:v1:idempotency:*")[0]
    assert 0 < client_a.ttl(idempotency_key) <= 60
    client_a.script_flush()
    stores[0].store(_principal(), "responses", "idem-after-flush", body, {"id": "third"})
    with pytest.raises(MixAPIError) as captured:
        stores[1].replay(
            _principal(),
            "responses",
            "idem",
            {"model": "gpt-5.5", "input": "different"},
        )
    assert captured.value.code == "idempotency_key_reused"


def test_quota_reservations_are_atomic_across_instances(redis_runtime_services) -> None:
    client_a, client_b, _durable, _pool = redis_runtime_services
    quotas = (
        RedisQuotaService(client_a, namespace="test", request_limit=1, token_limit=10),
        RedisQuotaService(client_b, namespace="test", request_limit=1, token_limit=10),
    )

    quotas[0].reserve_request(_principal())
    with pytest.raises(MixAPIError) as request_error:
        quotas[1].reserve_request(_principal())
    assert request_error.value.code == "request_quota_exceeded"

    def reserve(quota: RedisQuotaService):
        try:
            return quota.reserve_tokens(_principal(), 6)
        except MixAPIError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        reservations = list(executor.map(reserve, quotas))
    assert sum(item is not None for item in reservations) == 1
    accepted = next(item for item in reservations if item is not None)
    quotas[0].reconcile_tokens(accepted, 4)
    followup = quotas[1].reserve_tokens(_principal(), 6)
    quotas[1].release_tokens(followup)


def test_budget_reservation_and_reconciliation_are_atomic_and_idempotent(
    redis_runtime_services,
) -> None:
    client_a, client_b, durable, pool = redis_runtime_services
    budgets = (
        RedisBudgetService(client_a, durable, namespace="test", limit_usd=Decimal("1.00")),
        RedisBudgetService(client_b, durable, namespace="test", limit_usd=Decimal("1.00")),
    )

    def reserve(budget: RedisBudgetService):
        try:
            return budget.reserve(_principal(), Decimal("0.60"))
        except MixAPIError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        reservations = list(executor.map(reserve, budgets))
    assert sum(item is not None for item in reservations) == 1
    reservation = next(item for item in reservations if item is not None)

    budgets[0].reconcile(reservation, Decimal("0.50"))
    budgets[1].reconcile(reservation, Decimal("0.50"))

    assert durable.actual_spend("key_a") == Decimal("0.50000000")
    with pool.connection() as connection:
        outbox = connection.execute(
            """
            SELECT processed_at FROM budget_reconciliation_outbox
            WHERE reservation_id = %s
            """,
            (reservation.reservation_id,),
        ).fetchone()
    assert outbox["processed_at"] is not None
    with pytest.raises(MixAPIError) as denied:
        budgets[1].reserve(_principal(), Decimal("0.60"))
    assert denied.value.code == "api_key_budget_exceeded"


def test_circuit_state_is_shared_and_recovers_after_timeout(redis_runtime_services) -> None:
    client_a, client_b, _durable, _pool = redis_runtime_services
    first = RedisCircuitBreaker(
        client_a,
        namespace="test",
        failure_threshold=2,
        recovery_timeout_seconds=0.05,
    )
    second = RedisCircuitBreaker(
        client_b,
        namespace="test",
        failure_threshold=2,
        recovery_timeout_seconds=0.05,
    )

    first.record_failure("provider_a", "model_a", "upstream_timeout")
    second.record_failure("provider_a", "model_a", "upstream_http_500")
    assert first.is_open("provider_a", "model_a") is True
    assert first.is_open("provider_b", "model_a") is False

    import time

    time.sleep(0.07)
    assert second.is_open("provider_a", "model_a") is False


def test_redis_outage_fails_closed_without_local_fallback(database_url: str) -> None:
    unavailable = redis.Redis.from_url(
        "redis://127.0.0.1:1/0",
        decode_responses=True,
        socket_connect_timeout=0.05,
        socket_timeout=0.05,
    )
    idempotency = RedisIdempotencyStore(unavailable, namespace="test", ttl_seconds=60)

    with pytest.raises(MixAPIError) as captured:
        idempotency.replay(_principal(), "responses", "idem", {"input": "x"})

    assert captured.value.status_code == 503
    assert captured.value.code == "control_plane_unavailable"
    unavailable.close()
