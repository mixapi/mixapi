from __future__ import annotations

import os
import sys
import time

import psycopg
import redis


DEFAULT_DATABASE_URL = "postgresql://mixapi:mixapi@127.0.0.1:55432/mixapi_test"
DEFAULT_REDIS_URL = "redis://127.0.0.1:56379/0"
ATTEMPTS = 60
RETRY_SECONDS = 0.5


def main() -> None:
    database_url = os.getenv("MIXAPI_DATABASE_URL", DEFAULT_DATABASE_URL)
    redis_url = os.getenv("MIXAPI_REDIS_URL", DEFAULT_REDIS_URL)
    redis_client = redis.Redis.from_url(redis_url)

    last_error: Exception | None = None
    for _ in range(ATTEMPTS):
        try:
            with psycopg.connect(database_url, connect_timeout=2) as connection:
                connection.execute("SELECT 1")
            if redis_client.ping() is not True:
                raise RuntimeError("Redis PING did not return true")
            print("PostgreSQL and Redis are ready")
            return
        except (psycopg.Error, redis.RedisError, RuntimeError) as error:
            last_error = error
            time.sleep(RETRY_SECONDS)

    print(f"Dependencies did not become ready: {last_error}", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
