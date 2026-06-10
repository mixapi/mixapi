from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from mixapi.publication import ConfigurationPublisher, PublicationError


class OutboxRepository(Protocol):
    def claim_outbox(
        self,
        worker_id: str,
        *,
        limit: int,
        lease_seconds: int,
    ) -> list[Any]: ...

    def release_outbox(self, event_id: int, worker_id: str, *, error: str) -> bool: ...


class ConfigurationOutboxWorker:
    def __init__(
        self,
        repository: OutboxRepository,
        publisher: ConfigurationPublisher,
        *,
        worker_id: str,
        claim_limit: int = 10,
        lease_seconds: int = 30,
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._worker_id = worker_id
        self._claim_limit = claim_limit
        self._lease_seconds = lease_seconds

    def run_once(self) -> int:
        events = self._repository.claim_outbox(
            self._worker_id,
            limit=self._claim_limit,
            lease_seconds=self._lease_seconds,
        )
        processed = 0
        seen_versions: set[int] = set()
        for event in events:
            if event.configuration_version in seen_versions:
                continue
            seen_versions.add(event.configuration_version)
            try:
                self._publisher.publish(
                    event.configuration_version,
                    event_id=event.id,
                    worker_id=self._worker_id,
                )
            except PublicationError:
                processed += 1
            except Exception as error:
                self._repository.release_outbox(
                    event.id,
                    self._worker_id,
                    error=_safe_worker_error(error),
                )
            else:
                processed += 1
        return processed


class ConfigurationRebuildWorker:
    def __init__(self, publisher: ConfigurationPublisher) -> None:
        self._publisher = publisher

    def run_once(self) -> bool:
        return self._publisher.rebuild()


@dataclass(frozen=True)
class WorkerJob:
    name: str
    interval_seconds: float
    action: Callable[[], object]
    run_immediately: bool = True


class ManagedWorkers:
    def __init__(self, jobs: tuple[WorkerJob, ...], *, shutdown_timeout_seconds: float) -> None:
        self._jobs = jobs
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self.last_errors: dict[str, str] = {}

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._run(job), name=f"mixapi-{job.name}")
            for job in self._jobs
        ]

    async def stop(self) -> None:
        if not self._tasks:
            return
        self._stop.set()
        tasks = self._tasks
        self._tasks = []
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self._shutdown_timeout_seconds,
            )
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, job: WorkerJob) -> None:
        if not job.run_immediately:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=job.interval_seconds)
            except TimeoutError:
                pass
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(job.action)
                self.last_errors.pop(job.name, None)
            except Exception as error:
                self.last_errors[job.name] = _safe_worker_error(error)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=job.interval_seconds)
            except TimeoutError:
                pass


def _safe_worker_error(error: Exception) -> str:
    if isinstance(error, PublicationError):
        return f"{error.code}: {error.message}"[:500]
    return f"{type(error).__name__}: worker operation failed"[:500]
