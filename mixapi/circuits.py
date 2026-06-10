from __future__ import annotations

from typing import Protocol


class CircuitBreaker(Protocol):
    def is_open(self, provider: str, provider_model: str) -> bool: ...

    def record_failure(self, provider: str, provider_model: str, reason: str) -> None: ...

    def record_success(self, provider: str, provider_model: str) -> None: ...


def is_transient_failure(reason: str) -> bool:
    if reason in {
        "configured_provider_failure",
        "upstream_timeout",
        "upstream_network_error",
        "upstream_http_429",
    }:
        return True
    if not reason.startswith("upstream_http_"):
        return False
    try:
        return int(reason.removeprefix("upstream_http_")) >= 500
    except ValueError:
        return False
