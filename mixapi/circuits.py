from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Callable, Protocol


@dataclass
class CircuitState:
    consecutive_failures: int = 0
    opened_at: float | None = None


class CircuitBreaker(Protocol):
    def is_open(self, provider: str, provider_model: str) -> bool: ...

    def record_failure(self, provider: str, provider_model: str, reason: str) -> None: ...

    def record_success(self, provider: str, provider_model: str) -> None: ...


@dataclass
class InMemoryCircuitBreaker:
    failure_threshold: int = 3
    recovery_timeout_seconds: float = 30.0
    now: Callable[[], float] = monotonic
    _states: dict[tuple[str, str], CircuitState] = field(default_factory=dict)

    def is_open(self, provider: str, provider_model: str) -> bool:
        key = (provider, provider_model)
        state = self._states.get(key)
        if state is None or state.opened_at is None:
            return False
        if self.now() - state.opened_at >= self.recovery_timeout_seconds:
            self._states.pop(key, None)
            return False
        return True

    def record_failure(self, provider: str, provider_model: str, reason: str) -> None:
        if not is_transient_failure(reason):
            return
        key = (provider, provider_model)
        state = self._states.setdefault(key, CircuitState())
        state.consecutive_failures += 1
        if state.consecutive_failures >= self.failure_threshold:
            state.opened_at = self.now()

    def record_success(self, provider: str, provider_model: str) -> None:
        self._states.pop((provider, provider_model), None)


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
