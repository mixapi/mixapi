from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from mixapi.adapter_factory import AdapterFactory, SnapshotAdapterResolver
from mixapi.models import LogicalModel
from mixapi.observability import Observability, VersionedObservability
from mixapi.routing_catalog import catalog_from_snapshot
from mixapi.snapshots import ConfigurationSnapshot


@dataclass(frozen=True)
class RequestRuntimeContext:
    request_id: str
    trace_id: str
    snapshot: ConfigurationSnapshot
    catalog: Mapping[str, LogicalModel]
    adapters: SnapshotAdapterResolver
    observability: Observability

    @property
    def configuration_version(self) -> int:
        return self.snapshot.version

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        trace_id: str,
        snapshot: ConfigurationSnapshot,
        adapter_factory: AdapterFactory,
        observability: Observability,
    ) -> RequestRuntimeContext:
        return cls(
            request_id=request_id,
            trace_id=trace_id,
            snapshot=snapshot,
            catalog=MappingProxyType(catalog_from_snapshot(snapshot)),
            adapters=adapter_factory.for_snapshot(snapshot),
            observability=VersionedObservability(observability, snapshot.version),
        )
