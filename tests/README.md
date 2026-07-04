# PostgreSQL MCP Tests

This directory contains tests for the PostgreSQL MCP package.

## Running Tests

To run all tests:

```bash
uv run pytest
```

To run a specific test file:

```bash
uv run pytest tests/unit/sql/test_obfuscate_password.py
```

To run a specific test:

```bash
uv run pytest tests/unit/sql/test_db_conn_pool.py::test_pool_connect_success
```

## Test Structure

- **Unit Tests** (`tests/unit/`): Tests for individual components and functions
  - `sql/test_obfuscate_password.py`: Tests for password obfuscation functionality
  - `sql/test_db_conn_pool.py`: Tests for the database connection pool
  - `sql/test_db_conn_pool_registry.py`: Tests for the connection-pool registry — pools keyed by `(environment, database)`, non-fatal environment registration, scoped `reconnect`, and lazy re-check
  - `sql/test_sql_driver.py`: Tests for the SQL driver and transaction handling
- **Integration Tests** (`tests/integration/`): Docker-backed tests (skipped when Docker is unavailable)
  - `test_multi_database.py`: one PG host exposing several databases via `--databases`
  - `test_multi_environment.py`: multiple PG servers via `run_multi` — non-fatal startup and `reconnect` recovery (one container paused mid-test); needs no real Aiven host or VPN
