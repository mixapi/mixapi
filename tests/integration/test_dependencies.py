from __future__ import annotations

import psycopg
import redis


def test_postgres_is_required(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)


def test_redis_is_required(redis_url: str) -> None:
    assert redis.Redis.from_url(redis_url).ping() is True
