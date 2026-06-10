from __future__ import annotations

import os

import pytest


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
