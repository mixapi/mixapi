from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Mapping, Protocol


AttributeValue = str | int | float | bool
Labels = tuple[tuple[str, str], ...]
Attributes = tuple[tuple[str, AttributeValue], ...]


@dataclass(frozen=True)
class MetricSample:
    name: str
    value: float
    labels: Labels


@dataclass(frozen=True)
class SpanRecord:
    name: str
    trace_id: str
    status: str
    duration_ms: float
    attributes: Attributes


class Observability(Protocol):
    def increment_counter(
        self,
        name: str,
        labels: Mapping[str, object],
        amount: float = 1,
    ) -> None: ...

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: Mapping[str, object],
    ) -> None: ...

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: Mapping[str, object],
    ) -> None: ...

    def record_span(
        self,
        name: str,
        trace_id: str,
        status: str,
        duration_ms: float,
        attributes: Mapping[str, AttributeValue],
    ) -> None: ...


@dataclass
class InMemoryObservability:
    _counters: dict[tuple[str, Labels], float] = field(default_factory=dict)
    _histograms: list[MetricSample] = field(default_factory=list)
    _gauges: dict[tuple[str, Labels], float] = field(default_factory=dict)
    _spans: list[SpanRecord] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def increment_counter(
        self,
        name: str,
        labels: Mapping[str, object],
        amount: float = 1,
    ) -> None:
        normalized = _normalize_labels(labels)
        with self._lock:
            key = (name, normalized)
            self._counters[key] = self._counters.get(key, 0) + amount

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: Mapping[str, object],
    ) -> None:
        sample = MetricSample(name, value, _normalize_labels(labels))
        with self._lock:
            self._histograms.append(sample)

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: Mapping[str, object],
    ) -> None:
        normalized = _normalize_labels(labels)
        with self._lock:
            self._gauges[(name, normalized)] = value

    def record_span(
        self,
        name: str,
        trace_id: str,
        status: str,
        duration_ms: float,
        attributes: Mapping[str, AttributeValue],
    ) -> None:
        span = SpanRecord(
            name=name,
            trace_id=trace_id,
            status=status,
            duration_ms=duration_ms,
            attributes=tuple(sorted(attributes.items())),
        )
        with self._lock:
            self._spans.append(span)

    def counter_samples(self) -> tuple[MetricSample, ...]:
        with self._lock:
            return tuple(
                MetricSample(name, value, labels)
                for (name, labels), value in sorted(self._counters.items())
            )

    def histogram_samples(self) -> tuple[MetricSample, ...]:
        with self._lock:
            return tuple(self._histograms)

    def gauge_samples(self) -> tuple[MetricSample, ...]:
        with self._lock:
            return tuple(
                MetricSample(name, value, labels)
                for (name, labels), value in sorted(self._gauges.items())
            )

    def spans(self) -> tuple[SpanRecord, ...]:
        with self._lock:
            return tuple(self._spans)


def _normalize_labels(labels: Mapping[str, object]) -> Labels:
    return tuple(sorted((key, str(value)) for key, value in labels.items()))
