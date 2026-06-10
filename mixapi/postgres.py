from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool

from mixapi.settings import Settings


class DependencyUnavailable(RuntimeError):
    pass


class PostgresPool:
    def __init__(self, settings: Settings) -> None:
        self._open_timeout = settings.dependency_connect_timeout_seconds
        self._pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=settings.postgres_pool_min_size,
            max_size=settings.postgres_pool_max_size,
            timeout=settings.dependency_connect_timeout_seconds,
            open=False,
        )

    @property
    def closed(self) -> bool:
        return self._pool.closed

    def open(self) -> None:
        try:
            self._pool.open(wait=True, timeout=self._open_timeout)
        except Exception as error:
            self._pool.close()
            raise DependencyUnavailable("PostgreSQL is unavailable") from error

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        with self._pool.connection() as connection:
            yield connection

    def check(self) -> None:
        try:
            self._pool.check()
        except Exception as error:
            raise DependencyUnavailable("PostgreSQL is unavailable") from error
