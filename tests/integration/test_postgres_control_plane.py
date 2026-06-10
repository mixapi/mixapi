from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg
import pytest
import redis

from mixapi.auth_cache import RedisAuthCache
from mixapi.errors import MixAPIError
from mixapi.postgres import PostgresPool
from mixapi.repositories.control_plane import PostgresControlPlaneStore
from mixapi.settings import Settings


MASTER_KEY = bytes.fromhex("55" * 32)


@pytest.fixture
def postgres_control_plane(database_url: str, redis_url: str):
    settings = Settings(
        database_url=database_url,
        redis_url=redis_url,
        master_key=MASTER_KEY,
        master_key_version=1,
        previous_master_keys={},
        auth_cache_ttl_seconds=60,
    )
    pool = PostgresPool(settings)
    pool.open()
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    client.flushdb()
    with pool.connection() as connection:
        connection.execute(
            "TRUNCATE api_keys, tenant_policies, audit_events RESTART IDENTITY CASCADE"
        )
    cache = RedisAuthCache(client, ttl_seconds=60)
    store = PostgresControlPlaneStore(pool, auth_cache=cache)
    try:
        yield store, cache, pool, client
    finally:
        client.flushdb()
        client.close()
        pool.close()


def _create_key(store: PostgresControlPlaneStore):
    return store.create_api_key(
        tenant_id="tenant_acme",
        project_id="project_chat",
        name="chat service",
        scopes=("models:read", "responses:create"),
        model_allowlist=("gpt-5.5", "embed-portable"),
        budget_limit_usd=Decimal("2.50"),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        actor_id="admin",
    )


def test_api_keys_are_hash_only_and_mutations_write_redacted_audits_atomically(
    postgres_control_plane,
) -> None:
    store, _cache, pool, _client = postgres_control_plane

    created = _create_key(store)
    updated = store.update_api_key(created.record.id, {"name": "renamed"}, actor_id="admin")
    revoked = store.revoke_api_key(created.record.id, actor_id="admin")

    with pool.connection() as connection:
        row = connection.execute(
            "SELECT * FROM api_keys WHERE id = %s",
            (created.record.id,),
        ).fetchone()
        audits = connection.execute(
            "SELECT * FROM audit_events WHERE tenant_id = 'tenant_acme' ORDER BY sequence_id"
        ).fetchall()

    assert created.secret.startswith("mxapi_")
    assert row["key_hash"] != created.secret
    assert created.secret not in json.dumps([audit["after"] for audit in audits])
    assert "key_hash" not in json.dumps([audit["after"] for audit in audits])
    assert updated.name == "renamed"
    assert revoked.status == "revoked"
    assert [audit["action"] for audit in audits] == [
        "api_key.created",
        "api_key.updated",
        "api_key.revoked",
    ]


def test_expiry_and_tenant_policy_intersection_are_loaded_together(
    postgres_control_plane,
) -> None:
    store, _cache, _pool, _client = postgres_control_plane
    created = _create_key(store)
    store.set_tenant_model_allowlist(
        "tenant_acme",
        ("gpt-5.5",),
        actor_id="admin",
    )
    store.set_tenant_routing_policy(
        "tenant_acme",
        "lowest-cost",
        actor_id="admin",
    )

    record = store.resolve_api_key(created.secret)
    policy = store.get_tenant_policy("tenant_acme")
    store.update_api_key(
        created.record.id,
        {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
        actor_id="admin",
    )

    assert record is not None
    assert policy.model_allowlist == ("gpt-5.5",)
    assert policy.routing_objective == "lowest-cost"
    assert store.resolve_api_key(created.secret) is None


def test_redis_auth_cache_hit_does_not_call_postgres_loader(postgres_control_plane) -> None:
    store, cache, _pool, client = postgres_control_plane
    created = _create_key(store)
    calls = 0

    def load_authentication():
        nonlocal calls
        calls += 1
        record = store.resolve_api_key(created.secret)
        assert record is not None
        return record, store.get_tenant_policy(record.tenant_id)

    first = cache.resolve(created.secret, load_authentication)
    second = cache.resolve(created.secret, load_authentication)

    assert first == second
    assert calls == 1
    assert created.secret not in str(first)
    cache_key = client.keys("mixapi:auth:v1:key:*")[0]
    cached_payload = client.get(cache_key)
    assert "key_hash" not in cached_payload
    assert "key_prefix" not in cached_payload
    assert "name" not in cached_payload


def test_revocation_evicts_cached_authentication_immediately(postgres_control_plane) -> None:
    store, cache, _pool, _client = postgres_control_plane
    created = _create_key(store)
    cached = cache.resolve(
        created.secret,
        lambda: (created.record, store.get_tenant_policy(created.record.tenant_id)),
    )
    assert cached is not None

    store.revoke_api_key(created.record.id, actor_id="admin")
    loaded = cache.resolve(
        created.secret,
        lambda: None,
    )

    assert loaded is None


def test_redis_failure_fails_closed_without_calling_postgres_loader() -> None:
    unavailable = redis.Redis.from_url(
        "redis://127.0.0.1:1/0",
        decode_responses=True,
        socket_connect_timeout=0.05,
        socket_timeout=0.05,
    )
    cache = RedisAuthCache(unavailable, ttl_seconds=60)
    loaded = False

    def loader():
        nonlocal loaded
        loaded = True
        return None

    with pytest.raises(MixAPIError) as captured:
        cache.resolve("mxapi_secret", loader)

    assert captured.value.code == "control_plane_unavailable"
    assert captured.value.status_code == 503
    assert loaded is False
    unavailable.close()


def test_concurrent_updates_each_commit_with_one_audit(postgres_control_plane) -> None:
    store, _cache, pool, _client = postgres_control_plane
    created = _create_key(store)

    with ThreadPoolExecutor(max_workers=2) as executor:
        records = list(
            executor.map(
                lambda name: store.update_api_key(
                    created.record.id,
                    {"name": name},
                    actor_id="admin",
                ),
                ("worker-a", "worker-b"),
            )
        )

    with pool.connection() as connection:
        audit_count = connection.execute(
            "SELECT count(*) AS count FROM audit_events WHERE target_id = %s",
            (created.record.id,),
        ).fetchone()["count"]

    assert {record.name for record in records} == {"worker-a", "worker-b"}
    assert audit_count == 3
