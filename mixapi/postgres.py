from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from mixapi.settings import Settings


class DependencyUnavailable(RuntimeError):
    pass


class PostgresPool:
    def __init__(self, settings: Settings) -> None:
        self._conninfo = settings.database_url
        self._open_timeout = settings.dependency_connect_timeout_seconds
        self._ever_opened = False
        self._pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=settings.postgres_pool_min_size,
            max_size=settings.postgres_pool_max_size,
            timeout=settings.dependency_connect_timeout_seconds,
            kwargs={"row_factory": dict_row},
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
        self._ever_opened = True

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        if self._pool.closed and not self._ever_opened:
            # Repository-level tests and scripts may use stores without an ASGI lifespan.
            with psycopg.connect(self._conninfo, row_factory=dict_row) as connection:
                yield connection
            return
        with self._pool.connection() as connection:
            yield connection

    def check(self) -> None:
        try:
            self._pool.check()
        except Exception as error:
            raise DependencyUnavailable("PostgreSQL is unavailable") from error
