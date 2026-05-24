import asyncio
import sys
from typing import Generator

import pytest
from dotenv import load_dotenv
from utils import create_postgres_container

from postgres_mcp.sql import reset_postgres_version_cache

load_dotenv()


# Define a custom event loop policy that handles cleanup better
@pytest.fixture(scope="session")
def event_loop_policy():
    """Create and return a custom event loop policy for tests."""
    if sys.platform == "win32":
        # psycopg's async pool cannot run on Windows' default ProactorEventLoop; force Selector.
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="class", params=["postgres:12", "postgres:15", "postgres:16"])
def test_postgres_connection_string(request) -> Generator[tuple[str, str], None, None]:
    yield from create_postgres_container(request.param)


@pytest.fixture(scope="class")
def multi_db_connection_string(test_postgres_connection_string) -> Generator[tuple[str, str], None, None]:
    """Create extra databases (orders, catalog) on the test container for multi-DB tests."""
    conn_str, version = test_postgres_connection_string  # base points at test_db
    import psycopg

    with psycopg.connect(conn_str, autocommit=True) as conn:  # autocommit: CREATE DATABASE
        for name in ("orders", "catalog"):
            conn.execute(f'CREATE DATABASE "{name}"')
    yield conn_str, version


@pytest.fixture(autouse=True)
def reset_pg_version_cache():
    """Reset the PostgreSQL version cache before each test."""
    reset_postgres_version_cache()
    yield
