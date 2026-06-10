from __future__ import annotations

from typing import Any

import redis

from mixapi.errors import control_plane_unavailable


class LuaScript:
    def __init__(self, client: redis.Redis, source: str) -> None:
        self._client = client
        self._source = source
        self._sha: str | None = None

    def __call__(self, keys: list[str], args: list[Any]) -> Any:
        try:
            if self._sha is None:
                self._sha = self._client.script_load(self._source)
            try:
                return self._client.evalsha(self._sha, len(keys), *keys, *args)
            except redis.exceptions.NoScriptError:
                self._sha = self._client.script_load(self._source)
                return self._client.evalsha(self._sha, len(keys), *keys, *args)
        except (redis.RedisError, OSError) as error:
            raise control_plane_unavailable() from error
