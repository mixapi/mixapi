from __future__ import annotations

from collections import deque
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


@dataclass(frozen=True)
class SafeObservability:
    delegate: Observability

    def increment_counter(
        self,
        name: str,
        labels: Mapping[str, object],
        amount: float = 1,
    ) -> None:
        try:
            self.delegate.increment_counter(name, labels, amount)
        except Exception:
            pass

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: Mapping[str, object],
    ) -> None:
        try:
            self.delegate.observe_histogram(name, value, labels)
        except Exception:
            pass

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: Mapping[str, object],
    ) -> None:
        try:
            self.delegate.set_gauge(name, value, labels)
        except Exception:
            pass

    def record_span(
        self,
        name: str,
        trace_id: str,
        status: str,
        duration_ms: float,
        attributes: Mapping[str, AttributeValue],
    ) -> None:
        try:
            self.delegate.record_span(name, trace_id, status, duration_ms, attributes)
        except Exception:
            pass


@dataclass(frozen=True)
class VersionedObservability:
    delegate: Observability
    configuration_version: int

    def increment_counter(
        self,
        name: str,
        labels: Mapping[str, object],
        amount: float = 1,
    ) -> None:
        self.delegate.increment_counter(name, labels, amount)

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: Mapping[str, object],
    ) -> None:
        self.delegate.observe_histogram(name, value, labels)

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: Mapping[str, object],
    ) -> None:
        self.delegate.set_gauge(name, value, labels)

    def record_span(
        self,
        name: str,
        trace_id: str,
        status: str,
        duration_ms: float,
        attributes: Mapping[str, AttributeValue],
    ) -> None:
        self.delegate.record_span(
            name,
            trace_id,
            status,
            duration_ms,
            {**attributes, "configuration_version": self.configuration_version},
        )


@dataclass
class InMemoryObservability:
    max_records: int = 10_000
    _counters: dict[tuple[str, Labels], float] = field(default_factory=dict)
    _histograms: deque[MetricSample] = field(init=False)
    _gauges: dict[tuple[str, Labels], float] = field(default_factory=dict)
    _spans: deque[SpanRecord] = field(init=False)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def __post_init__(self) -> None:
        if self.max_records <= 0:
            raise ValueError("max_records must be positive")
        self._histograms = deque(maxlen=self.max_records)
        self._spans = deque(maxlen=self.max_records)

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
