from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from mixapi.auth import Principal


@dataclass(frozen=True)
class IdempotencyReplay:
    response: dict[str, Any]


class IdempotencyStore(Protocol):
    def replay(
        self,
        principal: Principal,
        endpoint: str,
        key: str | None,
        request_body: dict[str, Any],
    ) -> IdempotencyReplay | None: ...

    def store(
        self,
        principal: Principal,
        endpoint: str,
        key: str | None,
        request_body: dict[str, Any],
        response: dict[str, Any],
    ) -> None: ...


def request_hash(request_body: dict[str, Any]) -> str:
    encoded = json.dumps(request_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
