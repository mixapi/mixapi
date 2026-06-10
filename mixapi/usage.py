from __future__ import annotations

import base64
import binascii
import csv
import io
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class UsageEvent:
    request_id: str
    tenant_id: str
    project_id: str
    api_key_id: str
    endpoint: str
    logical_model: str
    provider: str
    provider_model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sequence_id: int | None = field(default=None, compare=False, repr=False)
    provider_connection_id: str | None = None
    configuration_version: int = 0

    def public_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "api_key_id": self.api_key_id,
            "endpoint": self.endpoint,
            "logical_model": self.logical_model,
            "provider": self.provider,
            "provider_model": self.provider_model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": _format_cost(self.cost_usd),
            "created_at": format_usage_timestamp(self.created_at),
        }


@dataclass(frozen=True)
class UsageQuery:
    tenant_id: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    limit: int | None = 50
    after_sequence_id: int = 0
    after_created_at: datetime | None = None


@dataclass(frozen=True)
class UsageWriteIntent:
    request_id: str
    tenant_id: str
    project_id: str
    api_key_id: str
    endpoint: str
    logical_model: str
    configuration_version: int
    reservation_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UsagePage:
    events: list[UsageEvent]
    has_more: bool


class UsageQueryValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class InMemoryUsageLedger:
    _events: list[UsageEvent] = field(default_factory=list)
    _next_sequence_id: int = 1
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def record(self, event: UsageEvent) -> None:
        with self._lock:
            self._events.append(replace(event, sequence_id=self._next_sequence_id))
            self._next_sequence_id += 1

    def events(self, tenant_id: str | None = None) -> list[UsageEvent]:
        with self._lock:
            if tenant_id is None:
                return list(self._events)
            return [event for event in self._events if event.tenant_id == tenant_id]

    def query(self, query: UsageQuery) -> UsagePage:
        with self._lock:
            matching = sorted(
                [
                    event
                    for event in self._events
                    if event.tenant_id == query.tenant_id
                    and (query.start_time is None or event.created_at >= query.start_time)
                    and (query.end_time is None or event.created_at <= query.end_time)
                    and event.sequence_id is not None
                    and _after_usage_cursor(event, query)
                ],
                key=lambda event: (event.created_at, event.sequence_id or 0),
            )
        if query.limit is None:
            return UsagePage(events=matching, has_more=False)
        has_more = len(matching) > query.limit
        return UsagePage(events=matching[: query.limit], has_more=has_more)


def usage_response(
    events: list[UsageEvent],
    *,
    has_more: bool = False,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [event.public_dict() for event in events],
        "has_more": has_more,
        "next_cursor": next_cursor,
        "summary": {
            "request_count": len(events),
            "input_tokens": sum(event.input_tokens for event in events),
            "output_tokens": sum(event.output_tokens for event in events),
            "cost_usd": _format_cost(sum((event.cost_usd for event in events), Decimal("0"))),
        },
    }


def usage_csv(events: list[UsageEvent]) -> str:
    fieldnames = [
        "request_id",
        "tenant_id",
        "project_id",
        "api_key_id",
        "endpoint",
        "logical_model",
        "provider",
        "provider_model",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "created_at",
    ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(event.public_dict() for event in events)
    return output.getvalue()


def usage_jsonl(events: list[UsageEvent]) -> str:
    lines = [json.dumps(event.public_dict(), separators=(",", ":")) for event in events]
    return "".join(f"{line}\n" for line in lines)


def _format_cost(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.00000001")), "f")


def parse_usage_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Usage timestamps must include a UTC offset.")
    return parsed.astimezone(timezone.utc)


def format_usage_timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def build_usage_query(
    *,
    tenant_id: str,
    start_time: str | None,
    end_time: str | None,
    limit: str | int | None,
    cursor: str | None,
) -> UsageQuery:
    try:
        parsed_start = parse_usage_timestamp(start_time)
        parsed_end = parse_usage_timestamp(end_time)
    except (TypeError, ValueError) as error:
        raise UsageQueryValidationError(
            "invalid_usage_timestamp",
            "Usage timestamps must be valid RFC 3339 values with a UTC offset.",
        ) from error

    if parsed_start is not None and parsed_end is not None and parsed_start > parsed_end:
        raise UsageQueryValidationError(
            "invalid_usage_time_range",
            "start_time must be earlier than or equal to end_time.",
        )

    parsed_limit = _parse_usage_limit(limit)
    after_sequence_id = 0
    after_created_at = None
    if cursor is not None:
        after_created_at, after_sequence_id = _decode_usage_cursor(
            cursor,
            tenant_id=tenant_id,
            start_time=parsed_start,
            end_time=parsed_end,
        )

    return UsageQuery(
        tenant_id=tenant_id,
        start_time=parsed_start,
        end_time=parsed_end,
        limit=parsed_limit,
        after_sequence_id=after_sequence_id,
        after_created_at=after_created_at,
    )


def encode_usage_cursor(
    query: UsageQuery,
    after_sequence_id: int,
    *,
    after_created_at: datetime | None = None,
) -> str:
    payload = {
        "after": after_sequence_id,
        "after_created_at": _optional_timestamp(after_created_at),
        "end": _optional_timestamp(query.end_time),
        "start": _optional_timestamp(query.start_time),
        "tenant": query.tenant_id,
        "v": 1,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return encoded.decode("ascii").rstrip("=")


def _parse_usage_limit(value: str | int | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise UsageQueryValidationError(
            "invalid_usage_limit",
            "limit must be an integer between 1 and 100.",
        ) from error
    if parsed < 1 or parsed > 100:
        raise UsageQueryValidationError(
            "invalid_usage_limit",
            "limit must be an integer between 1 and 100.",
        )
    return parsed


def _decode_usage_cursor(
    cursor: str,
    *,
    tenant_id: str,
    start_time: datetime | None,
    end_time: datetime | None,
) -> tuple[datetime | None, int]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
        after_sequence_id = payload["after"]
        raw_after_created_at = payload.get("after_created_at")
        after_created_at = parse_usage_timestamp(raw_after_created_at)
        valid = (
            payload.get("v") == 1
            and payload.get("tenant") == tenant_id
            and payload.get("start") == _optional_timestamp(start_time)
            and payload.get("end") == _optional_timestamp(end_time)
            and isinstance(after_sequence_id, int)
            and not isinstance(after_sequence_id, bool)
            and after_sequence_id > 0
            and after_created_at is not None
        )
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError):
        valid = False
        after_sequence_id = 0
        after_created_at = None
    if not valid:
        raise UsageQueryValidationError(
            "invalid_usage_cursor",
            "The usage cursor is invalid for this tenant or time window.",
        )
    return after_created_at, after_sequence_id


def _optional_timestamp(value: datetime | None) -> str | None:
    return format_usage_timestamp(value) if value is not None else None


def _after_usage_cursor(event: UsageEvent, query: UsageQuery) -> bool:
    if query.after_created_at is None:
        return event.sequence_id is not None and event.sequence_id > query.after_sequence_id
    return (event.created_at, event.sequence_id or 0) > (
        query.after_created_at,
        query.after_sequence_id,
    )
