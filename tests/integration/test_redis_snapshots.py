from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
import redis

from mixapi.configuration import StoredConfiguration
from mixapi.errors import MixAPIError
from mixapi.runtime.snapshots import RedisSnapshotStore, SnapshotConflict, SnapshotNotReady
from mixapi.snapshots import ConfigurationSnapshot


def _snapshot(version: int) -> ConfigurationSnapshot:
    return ConfigurationSnapshot.create(
        version,
        datetime(2026, 6, 10, 12, version, tzinfo=UTC),
        StoredConfiguration(),
    )


@pytest.fixture
def snapshot_stores(redis_url: str):
    clients = (
        redis.Redis.from_url(redis_url, decode_responses=True),
        redis.Redis.from_url(redis_url, decode_responses=True),
    )
    clients[0].flushdb()
    try:
        yield (
            RedisSnapshotStore(clients[0], namespace="test"),
            RedisSnapshotStore(clients[1], namespace="test"),
            clients[0],
        )
    finally:
        clients[0].flushdb()
        clients[0].close()
        clients[1].close()


def _publish(store: RedisSnapshotStore, snapshot: ConfigurationSnapshot) -> bool:
    store.write_pending(snapshot)
    store.mark_ready(snapshot.version, snapshot.checksum)
    return store.activate(snapshot.version, snapshot.checksum)


def test_snapshot_payloads_are_immutable_and_activation_requires_ready_manifest(
    snapshot_stores,
) -> None:
    store, _other, client = snapshot_stores
    snapshot = _snapshot(1)

    assert store.write_pending(snapshot) is True
    assert store.write_pending(snapshot) is False
    with pytest.raises(SnapshotNotReady):
        store.activate(snapshot.version, snapshot.checksum)

    changed = ConfigurationSnapshot.create(
        1,
        datetime(2026, 6, 10, 13, 1, tzinfo=UTC),
        StoredConfiguration(),
    )
    with pytest.raises(SnapshotConflict):
        store.write_pending(changed)

    store.mark_ready(snapshot.version, snapshot.checksum)
    assert store.activate(snapshot.version, snapshot.checksum) is True
    assert store.active_version() == 1
    assert store.load(1) == snapshot
    assert client.get("test:v1:snapshots:1:payload") == snapshot.to_json()


def test_activation_prevents_rollback_and_concurrent_publishers_converge_on_latest(
    snapshot_stores,
) -> None:
    first, second, _client = snapshot_stores
    snapshots = (_snapshot(2), _snapshot(3))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda pair: _publish(pair[0], pair[1]),
                ((first, snapshots[0]), (second, snapshots[1])),
            )
        )

    assert any(results)
    assert first.active_version() == 3
    assert first.activate(2, snapshots[0].checksum) is False


def test_retention_keeps_active_and_applies_ttl_to_previous_version(snapshot_stores) -> None:
    store, _other, client = snapshot_stores
    for version in (1, 2, 3):
        _publish(store, _snapshot(version))

    store.retain_versions(active_version=3, count=2, ttl_seconds=60)

    assert client.ttl("test:v1:snapshots:3:payload") == -1
    assert 0 < client.ttl("test:v1:snapshots:2:payload") <= 60
    assert client.exists("test:v1:snapshots:1:payload") == 0
    assert client.exists("test:v1:snapshots:1:manifest") == 0


def test_missing_or_corrupt_payload_fails_closed(snapshot_stores) -> None:
    store, _other, client = snapshot_stores
    snapshot = _snapshot(1)
    _publish(store, snapshot)
    client.delete("test:v1:snapshots:1:payload")

    with pytest.raises(MixAPIError) as missing:
        store.load(1)
    assert missing.value.code == "configuration_unavailable"

    client.set("test:v1:snapshots:1:payload", "{}")
    with pytest.raises(MixAPIError) as corrupt:
        store.load(1)
    assert corrupt.value.code == "configuration_unavailable"


def test_ready_manifest_rejects_missing_payload_and_wrong_checksum(snapshot_stores) -> None:
    store, _other, _client = snapshot_stores
    snapshot = _snapshot(1)

    with pytest.raises(SnapshotNotReady):
        store.mark_ready(1, snapshot.checksum)
    store.write_pending(snapshot)
    with pytest.raises(SnapshotConflict):
        store.mark_ready(1, "0" * 64)
