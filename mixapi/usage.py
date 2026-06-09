from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from decimal import Decimal
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
        }


@dataclass
class InMemoryUsageLedger:
    _events: list[UsageEvent] = field(default_factory=list)

    def record(self, event: UsageEvent) -> None:
        self._events.append(event)

    def events(self, tenant_id: str | None = None) -> list[UsageEvent]:
        if tenant_id is None:
            return list(self._events)
        return [event for event in self._events if event.tenant_id == tenant_id]


def usage_response(events: list[UsageEvent]) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [event.public_dict() for event in events],
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
    ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(event.public_dict() for event in events)
    return output.getvalue()


def _format_cost(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.00000001")), "f")
