from __future__ import annotations

import re

import redis

from mixapi.errors import configuration_unavailable, control_plane_unavailable
from mixapi.redis_scripts import LuaScript
from mixapi.snapshots import ConfigurationSnapshot, SnapshotValidationError


class SnapshotConflict(ValueError):
    pass


class SnapshotNotReady(ValueError):
    pass


_MARK_READY = """
local payload = redis.call('GET', KEYS[1])
if not payload then
  return -1
end
if payload ~= ARGV[1] then
  return -2
end
local manifest = redis.call('GET', KEYS[2])
if manifest and manifest ~= ARGV[2] then
  return -3
end
if manifest then
  return 0
end
redis.call('SET', KEYS[2], ARGV[2])
return 1
"""


_ACTIVATE = """
if redis.call('EXISTS', KEYS[3]) == 0 then
  return -1
end
local manifest = redis.call('GET', KEYS[2])
if not manifest then
  return -2
end
if manifest ~= ARGV[2] then
  return -3
end
local active = redis.call('GET', KEYS[1])
if active and not tonumber(active) then
  return -4
end
if active and tonumber(active) > tonumber(ARGV[1]) then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1])
return 1
"""


_RELEASE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisSnapshotStore:
    def __init__(self, client: redis.Redis, *, namespace: str) -> None:
        self._client = client
        self._prefix = f"{namespace}:v1:snapshots"
        self._mark_ready_script = LuaScript(client, _MARK_READY)
        self._activate_script = LuaScript(client, _ACTIVATE)
        self._release_lock_script = LuaScript(client, _RELEASE_LOCK)

    def write_pending(self, snapshot: ConfigurationSnapshot) -> bool:
        payload = snapshot.to_json()
        key = self._payload_key(snapshot.version)
        try:
            created = self._client.set(key, payload, nx=True)
            if created:
                return True
            existing = self._client.get(key)
        except (redis.RedisError, OSError) as error:
            raise control_plane_unavailable() from error
        if _as_text(existing) == payload:
            return False
        raise SnapshotConflict(
            f"Snapshot version {snapshot.version} already has a different payload"
        )

    def mark_ready(self, version: int, checksum: str) -> bool:
        snapshot = self.load(version, missing_is_not_ready=True)
        if snapshot.checksum != checksum:
            raise SnapshotConflict(f"Snapshot version {version} checksum does not match")
        result = self._mark_ready_script(
            [self._payload_key(version), self._manifest_key(version)],
            [snapshot.to_json(), checksum],
        )
        if result == -1:
            raise SnapshotNotReady(f"Snapshot version {version} has no payload")
        if result in (-2, -3):
            raise SnapshotConflict(f"Snapshot version {version} changed during publication")
        return result == 1

    def activate(self, version: int, checksum: str) -> bool:
        result = self._activate_script(
            [self._active_key(), self._manifest_key(version), self._payload_key(version)],
            [version, checksum],
        )
        if result in (-1, -2):
            raise SnapshotNotReady(f"Snapshot version {version} is not ready")
        if result == -3:
            raise SnapshotConflict(f"Snapshot version {version} checksum does not match")
        if result == -4:
            raise configuration_unavailable(
                message="The active snapshot pointer is invalid."
            )
        return result == 1

    def active_version(self) -> int | None:
        try:
            raw = self._client.get(self._active_key())
        except (redis.RedisError, OSError) as error:
            raise control_plane_unavailable() from error
        if raw is None:
            return None
        try:
            version = int(_as_text(raw))
        except (TypeError, ValueError) as error:
            raise configuration_unavailable(
                message="The active snapshot pointer is invalid."
            ) from error
        if version <= 0:
            raise configuration_unavailable(
                message="The active snapshot pointer is invalid."
            )
        return version

    def load(
        self,
        version: int,
        *,
        missing_is_not_ready: bool = False,
    ) -> ConfigurationSnapshot:
        try:
            raw = self._client.get(self._payload_key(version))
        except (redis.RedisError, OSError) as error:
            raise control_plane_unavailable() from error
        if raw is None:
            if missing_is_not_ready:
                raise SnapshotNotReady(f"Snapshot version {version} has no payload")
            raise configuration_unavailable(
                message=f"Routing configuration version {version} is unavailable."
            )
        try:
            snapshot = ConfigurationSnapshot.from_json(_as_text(raw))
        except SnapshotValidationError as error:
            raise configuration_unavailable(
                message=f"Routing configuration version {version} is invalid."
            ) from error
        if snapshot.version != version:
            raise configuration_unavailable(
                message=f"Routing configuration version {version} is invalid."
            )
        return snapshot

    def retain_versions(
        self,
        active_version: int,
        count: int,
        ttl_seconds: int,
    ) -> None:
        if count < 1:
            raise ValueError("Snapshot retention count must be positive")
        if ttl_seconds < 1:
            raise ValueError("Snapshot retention TTL must be positive")
        pattern = re.compile(rf"^{re.escape(self._prefix)}:(\d+):payload$")
        try:
            versions = []
            for raw_key in self._client.scan_iter(match=f"{self._prefix}:*:payload"):
                match = pattern.match(_as_text(raw_key))
                if match:
                    versions.append(int(match.group(1)))
            published = sorted(
                (version for version in versions if version <= active_version),
                reverse=True,
            )
            retained = set(published[:count])
            for version in published:
                keys = [self._payload_key(version), self._manifest_key(version)]
                if version == active_version:
                    self._client.persist(keys[0])
                    self._client.persist(keys[1])
                elif version in retained:
                    self._client.expire(keys[0], ttl_seconds)
                    self._client.expire(keys[1], ttl_seconds)
                else:
                    self._client.delete(*keys)
        except (redis.RedisError, OSError) as error:
            raise control_plane_unavailable() from error

    def acquire_rebuild_lock(self, owner: str, *, ttl_seconds: int) -> bool:
        try:
            return bool(
                self._client.set(
                    f"{self._prefix}:rebuild-lock",
                    owner,
                    nx=True,
                    ex=ttl_seconds,
                )
            )
        except (redis.RedisError, OSError) as error:
            raise control_plane_unavailable() from error

    def release_rebuild_lock(self, owner: str) -> bool:
        return bool(
            self._release_lock_script(
                [f"{self._prefix}:rebuild-lock"],
                [owner],
            )
        )

    def _active_key(self) -> str:
        return f"{self._prefix}:active"

    def _payload_key(self, version: int) -> str:
        return f"{self._prefix}:{version}:payload"

    def _manifest_key(self, version: int) -> str:
        return f"{self._prefix}:{version}:manifest"


def _as_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)
