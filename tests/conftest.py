from __future__ import annotations

import os

import pytest


os.environ.setdefault(
    "MIXAPI_DATABASE_URL",
    "postgresql://mixapi:mixapi@127.0.0.1:55432/mixapi_test",
)
os.environ.setdefault("MIXAPI_REDIS_URL", "redis://127.0.0.1:56379/0")
os.environ.setdefault(
    "MIXAPI_MASTER_KEY",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
)
os.environ.setdefault("MIXAPI_MASTER_KEY_VERSION", "1")


@pytest.fixture(scope="session")
def database_url() -> str:
    value = os.getenv("MIXAPI_DATABASE_URL")
    assert value, "MIXAPI_DATABASE_URL is required for the test suite"
    return value


@pytest.fixture(scope="session")
def redis_url() -> str:
    value = os.getenv("MIXAPI_REDIS_URL")
    assert value, "MIXAPI_REDIS_URL is required for the test suite"
    return value
