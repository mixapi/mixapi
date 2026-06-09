from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from mixapi.auth import Principal
from mixapi.errors import validation_error


@dataclass(frozen=True)
class IdempotencyReplay:
    response: dict[str, Any]


@dataclass
class IdempotencyRecord:
    request_hash: str
    response: dict[str, Any]


@dataclass
class InMemoryIdempotencyStore:
    _records: dict[tuple[str, str, str], IdempotencyRecord] = field(default_factory=dict)

    def replay(
        self,
        principal: Principal,
        endpoint: str,
        key: str | None,
        request_body: dict[str, Any],
    ) -> IdempotencyReplay | None:
        if not key:
            return None

        record_key = (principal.tenant_id, endpoint, key)
        request_hash_value = request_hash(request_body)
        record = self._records.get(record_key)
        if record is None:
            return None

        if record.request_hash != request_hash_value:
            raise validation_error(
                "idempotency_key_reused",
                "Idempotency key was reused with a different request body.",
            )

        return IdempotencyReplay(response=copy.deepcopy(record.response))

    def store(
        self,
        principal: Principal,
        endpoint: str,
        key: str | None,
        request_body: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        if not key:
            return

        record_key = (principal.tenant_id, endpoint, key)
        if record_key not in self._records:
            self._records[record_key] = IdempotencyRecord(
                request_hash=request_hash(request_body),
                response=copy.deepcopy(response),
            )


def request_hash(request_body: dict[str, Any]) -> str:
    encoded = json.dumps(request_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
