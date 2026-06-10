from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from mixapi.app import create_app
from mixapi.postgres import DependencyUnavailable
from mixapi.settings import ConfigurationError, Settings


MASTER_KEY = bytes.fromhex("00" * 32)


def _settings(database_url: str, redis_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        redis_url=redis_url,
        master_key=MASTER_KEY,
        master_key_version=1,
        previous_master_keys={},
        admin_api_key="admin-secret",
    )


def test_settings_require_database_redis_and_master_key(monkeypatch) -> None:
    for name in (
        "MIXAPI_DATABASE_URL",
        "MIXAPI_REDIS_URL",
        "MIXAPI_MASTER_KEY",
        "MIXAPI_MASTER_KEY_VERSION",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ConfigurationError, match="MIXAPI_DATABASE_URL"):
        Settings.from_env()


def test_unreachable_postgres_prevents_application_startup(
    database_url: str,
    redis_url: str,
) -> None:
    settings = replace(
        _settings(database_url, redis_url),
        database_url="postgresql://mixapi:mixapi@127.0.0.1:1/mixapi_test",
        dependency_connect_timeout_seconds=0.1,
    )
    app = create_app(settings=settings)

    with pytest.raises(DependencyUnavailable, match="PostgreSQL"):
        with TestClient(app):
            pass


def test_unreachable_redis_prevents_application_startup_and_closes_postgres(
    database_url: str,
    redis_url: str,
) -> None:
    settings = replace(
        _settings(database_url, redis_url),
        redis_url="redis://127.0.0.1:1/0",
        dependency_connect_timeout_seconds=0.1,
    )
    app = create_app(settings=settings)

    with pytest.raises(DependencyUnavailable, match="Redis"):
        with TestClient(app):
            pass

    assert app.state.postgres_pool.closed


def test_lifespan_opens_and_closes_dependency_clients(
    database_url: str,
    redis_url: str,
) -> None:
    app = create_app(settings=_settings(database_url, redis_url))

    assert app.state.postgres_pool.closed
    assert app.state.redis_runtime.closed

    with TestClient(app):
        assert not app.state.postgres_pool.closed
        assert not app.state.redis_runtime.closed
        assert app.state.redis_runtime.ping()

    assert app.state.postgres_pool.closed
    assert app.state.redis_runtime.closed
