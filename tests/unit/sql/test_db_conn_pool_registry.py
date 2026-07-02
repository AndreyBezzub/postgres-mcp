import asyncio
import sys
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

import postgres_mcp.server as server
from postgres_mcp.server import AccessMode
from postgres_mcp.server import get_sql_driver
from postgres_mcp.sql.db_conn_pool_registry import DEFAULT_ENV
from postgres_mcp.sql.db_conn_pool_registry import DatabaseValidationError
from postgres_mcp.sql.db_conn_pool_registry import DbConnPoolRegistry
from postgres_mcp.sql.db_conn_pool_registry import ValidationResult
from postgres_mcp.sql.safe_sql import SafeSqlDriver
from postgres_mcp.sql.sql_driver import SqlDriver

BASE_URL = "postgresql://postgres:secret@localhost:5432/test_db"


class _ACM:
    """Minimal async context manager yielding a fixed value."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


def _make_discovery_pool(returned_names):
    """Build a mock pool whose cursor.fetchall returns the given datnames."""
    cursor = AsyncMock()
    cursor.execute = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[(n,) for n in returned_names])

    connection = MagicMock()
    connection.cursor = MagicMock(return_value=_ACM(cursor))

    pool = MagicMock()
    pool.connection = MagicMock(return_value=_ACM(connection))
    return pool


def _make_pool_factory(discovery_returns):
    """Return a DbConnPool side_effect: first call = discovery, rest = lazy pools."""
    discovery_pool = _make_discovery_pool(discovery_returns)
    discovery_inst = MagicMock()
    discovery_inst.pool_connect = AsyncMock(return_value=discovery_pool)
    discovery_inst.close = AsyncMock()

    created = []

    def factory(url):
        if not factory.calls:
            factory.calls.append(url)
            return discovery_inst
        factory.calls.append(url)
        m = MagicMock()
        m.pool_connect = AsyncMock()
        m.close = AsyncMock()
        created.append(m)
        return m

    factory.calls = []
    return factory, discovery_inst, created


def test_build_db_url_swaps_dbname_keeps_creds_and_host():
    reg = DbConnPoolRegistry()
    reg._base_url = BASE_URL
    assert reg._build_db_url("orders") == "postgresql://postgres:secret@localhost:5432/orders"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("postgresql://postgres:secret@localhost:5432/", "postgres"),
        ("postgresql://postgres:secret@localhost:5432", "postgres"),
        ("postgresql://postgres:secret@localhost:5432/foo", "foo"),
    ],
)
def test_discovery_dbname(url, expected):
    reg = DbConnPoolRegistry()
    assert reg._discovery_dbname(url) == expected


@pytest.mark.asyncio
async def test_single_mode_registers_only_discovery_no_query():
    factory, discovery_inst, _ = _make_pool_factory([])
    with patch("postgres_mcp.sql.db_conn_pool_registry.DbConnPool", side_effect=factory):
        reg = DbConnPoolRegistry()
        result = await reg.validate_and_register(BASE_URL, None)

    assert reg.mode == "single"
    assert reg.get_names() == ["test_db"]
    assert result.registered == ["test_db"]
    assert result.missing == []
    # single mode never opens the discovery pool / runs no pg_database query
    discovery_inst.pool_connect.assert_not_called()


@pytest.mark.asyncio
async def test_multi_mode_registered_missing_split():
    factory, _, _ = _make_pool_factory(["orders", "catalog"])
    with patch("postgres_mcp.sql.db_conn_pool_registry.DbConnPool", side_effect=factory):
        reg = DbConnPoolRegistry()
        result = await reg.validate_and_register(BASE_URL, ["orders", "catalog", "ghost"])

    assert reg.mode == "multi"
    assert result.registered == ["orders", "catalog"]
    assert result.missing == ["ghost"]
    assert result.disallowed == []
    assert set(reg.get_names()) == {"orders", "catalog"}


@pytest.mark.asyncio
async def test_multi_mode_partial_validation():
    factory, _, _ = _make_pool_factory(["orders"])
    with patch("postgres_mcp.sql.db_conn_pool_registry.DbConnPool", side_effect=factory):
        reg = DbConnPoolRegistry()
        result = await reg.validate_and_register(BASE_URL, ["orders", "ghost"])

    assert result.registered == ["orders"]
    assert result.missing == ["ghost"]


@pytest.mark.asyncio
async def test_multi_mode_all_invalid():
    factory, discovery_inst, _ = _make_pool_factory([])
    with patch("postgres_mcp.sql.db_conn_pool_registry.DbConnPool", side_effect=factory):
        reg = DbConnPoolRegistry()
        result = await reg.validate_and_register(BASE_URL, ["ghost1", "ghost2"])

    assert result.registered == []
    assert result.missing == ["ghost1", "ghost2"]
    assert reg.get_names() == []
    # discovery pool is opened then closed, never retained
    discovery_inst.close.assert_awaited()


@pytest.mark.asyncio
async def test_get_pool_lazy_open():
    factory, _, created = _make_pool_factory(["orders"])
    with patch("postgres_mcp.sql.db_conn_pool_registry.DbConnPool", side_effect=factory):
        reg = DbConnPoolRegistry()
        await reg.validate_and_register(BASE_URL, ["orders"])
        lazy_pool = created[0]
        # registration alone does not open the pool
        lazy_pool.pool_connect.assert_not_called()

        returned = await reg.get_pool("orders")
        assert returned is lazy_pool
        lazy_pool.pool_connect.assert_called_once()


@pytest.mark.asyncio
async def test_get_pool_unknown_raises_with_message_and_available():
    """Unknown non-empty name → 'Unknown database ...' (disambiguated from the None case)."""
    factory, _, _ = _make_pool_factory(["orders", "catalog"])
    with patch("postgres_mcp.sql.db_conn_pool_registry.DbConnPool", side_effect=factory):
        reg = DbConnPoolRegistry()
        await reg.validate_and_register(BASE_URL, ["orders", "catalog"])

        with pytest.raises(DatabaseValidationError) as exc_info:
            await reg.get_pool("nope")

    err = exc_info.value
    assert "Unknown database 'nope'" in str(err)
    assert "Available databases: orders, catalog" in str(err)
    assert "Call list_databases" in str(err)
    assert err.available_databases == ["orders", "catalog"]


@pytest.mark.asyncio
async def test_get_pool_none_raises_required_message():
    """None/empty name → 'database_name is required' (disambiguated from the unknown case)."""
    factory, _, _ = _make_pool_factory(["orders", "catalog"])
    with patch("postgres_mcp.sql.db_conn_pool_registry.DbConnPool", side_effect=factory):
        reg = DbConnPoolRegistry()
        await reg.validate_and_register(BASE_URL, ["orders", "catalog"])
        with pytest.raises(DatabaseValidationError) as exc_info:
            await reg.get_pool(None)

    err = exc_info.value
    assert "database_name is required" in str(err)
    assert "Available databases: orders, catalog" in str(err)
    assert err.available_databases == ["orders", "catalog"]


@pytest.mark.asyncio
async def test_close_all_closes_every_pool():
    factory, _, created = _make_pool_factory(["orders", "catalog"])
    with patch("postgres_mcp.sql.db_conn_pool_registry.DbConnPool", side_effect=factory):
        reg = DbConnPoolRegistry()
        await reg.validate_and_register(BASE_URL, ["orders", "catalog"])
        await reg.close_all()

    for pool in created:
        pool.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_mode_default_get_sql_driver_resolves_sole_db():
    """get_sql_driver(None) resolves to the only registered DB in single mode."""
    mock_pool = MagicMock()
    reg = DbConnPoolRegistry()
    reg._mode = "single"
    # Registry now keys pools by (environment, database); single/multi path uses DEFAULT_ENV.
    reg._pools[(DEFAULT_ENV, "test_db")] = MagicMock()
    with (
        patch("postgres_mcp.server.db_registry", reg),
        patch.object(reg, "get_pool", AsyncMock(return_value=mock_pool)) as mock_get_pool,
        patch("postgres_mcp.server.current_access_mode", AccessMode.UNRESTRICTED),
    ):
        driver = await get_sql_driver(None)

    assert isinstance(driver, SqlDriver)
    assert not isinstance(driver, SafeSqlDriver)
    # get_sql_driver now forwards (database_name, environment) to the registry.
    mock_get_pool.assert_awaited_once_with("test_db", None)


@pytest.mark.asyncio
async def test_list_databases_return_shape():
    """The list_databases tool returns {"databases": [...], "mode": "single"|"multi"}."""
    reg = DbConnPoolRegistry()
    reg._mode = "single"
    reg._pools[(DEFAULT_ENV, "test_db")] = MagicMock()
    with patch("postgres_mcp.server.db_registry", reg):
        result = await server.list_databases()

    assert set(result.keys()) == {"databases", "mode"}
    assert result["databases"] == ["test_db"]
    assert result["mode"] in ("single", "multi")


@pytest.mark.asyncio
async def test_concurrent_get_pool_serialized_single_instance():
    """Concurrent first-calls for the same DB never open two pools (per-DB lock serializes)."""
    factory, _, created = _make_pool_factory(["orders"])
    with patch("postgres_mcp.sql.db_conn_pool_registry.DbConnPool", side_effect=factory):
        reg = DbConnPoolRegistry()
        await reg.validate_and_register(BASE_URL, ["orders"])
        lazy_pool = created[0]

        concurrency = {"current": 0, "max": 0}

        async def slow_connect(*_args, **_kwargs):
            concurrency["current"] += 1
            concurrency["max"] = max(concurrency["max"], concurrency["current"])
            await asyncio.sleep(0.01)
            concurrency["current"] -= 1

        lazy_pool.pool_connect = AsyncMock(side_effect=slow_connect)

        results = await asyncio.gather(*[reg.get_pool("orders") for _ in range(5)])

    assert all(r is lazy_pool for r in results)
    assert concurrency["max"] == 1  # lock prevented overlapping pool_connect calls
    assert lazy_pool.pool_connect.await_count == 5


@pytest.mark.asyncio
async def test_discovery_connection_failure_raises_validation_error():
    """A discovery-DB connect failure surfaces as DatabaseValidationError with a masked URL."""
    discovery_inst = MagicMock()
    discovery_inst.pool_connect = AsyncMock(side_effect=OSError("connection refused"))
    discovery_inst.close = AsyncMock()

    with patch("postgres_mcp.sql.db_conn_pool_registry.DbConnPool", return_value=discovery_inst):
        reg = DbConnPoolRegistry()
        with pytest.raises(DatabaseValidationError) as exc_info:
            await reg.validate_and_register(BASE_URL, ["orders"])

    assert "secret" not in str(exc_info.value)  # password masked in the URL
    assert "discovery database" in str(exc_info.value)
    discovery_inst.close.assert_awaited()  # finally still closes the discovery pool
    assert reg.get_names() == []


@pytest.mark.asyncio
async def test_lazy_open_failure_surfaces_as_validation_error():
    """A failure when lazily opening a registered pool surfaces as DatabaseValidationError."""
    factory, _, created = _make_pool_factory(["orders"])
    with patch("postgres_mcp.sql.db_conn_pool_registry.DbConnPool", side_effect=factory):
        reg = DbConnPoolRegistry()
        await reg.validate_and_register(BASE_URL, ["orders"])
        lazy_pool = created[0]
        lazy_pool.pool_connect = AsyncMock(side_effect=OSError("server closed the connection"))

        with pytest.raises(DatabaseValidationError) as exc_info:
            await reg.get_pool("orders")

    assert "orders" in str(exc_info.value)


@pytest.mark.asyncio
async def test_main_exits_when_all_databases_invalid(monkeypatch):
    """Multi-DB bootstrap with no valid databases exits with status 1."""
    monkeypatch.setattr(sys, "argv", ["postgres-mcp", BASE_URL, "--databases=ghost1,ghost2"])
    monkeypatch.delenv("DATABASE_URI", raising=False)

    fake_result = ValidationResult(registered=[], missing=["ghost1", "ghost2"])
    with (
        patch.object(server.mcp, "add_tool"),
        patch.object(server.db_registry, "validate_and_register", AsyncMock(return_value=fake_result)),
    ):
        with pytest.raises(SystemExit) as exc_info:
            await server.main()

    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_main_dedupes_databases(monkeypatch):
    """--databases entries are deduplicated (order preserved) before registration."""
    monkeypatch.setattr(sys, "argv", ["postgres-mcp", BASE_URL, "--databases=orders,orders,catalog,orders"])
    monkeypatch.delenv("DATABASE_URI", raising=False)

    captured = {}

    async def fake_validate(base_url, database_names):
        captured["names"] = database_names
        return ValidationResult(registered=list(database_names or []), missing=[])

    with (
        patch.object(server.mcp, "add_tool"),
        patch.object(server.db_registry, "validate_and_register", side_effect=fake_validate),
        patch.object(server.mcp, "run_stdio_async", AsyncMock()),
    ):
        await server.main()

    assert captured["names"] == ["orders", "catalog"]
