"""Docker-backed integration tests for multi-database support."""

from unittest.mock import patch

import pytest

import postgres_mcp.server as server
from postgres_mcp.server import AccessMode
from postgres_mcp.sql.db_conn_pool_registry import DEFAULT_ENV
from postgres_mcp.sql.db_conn_pool_registry import DbConnPoolRegistry
from postgres_mcp.sql.sql_driver import SqlDriver


@pytest.mark.asyncio
class TestMultiDatabase:
    async def test_validate_and_register_split(self, multi_db_connection_string):
        conn_str, _ = multi_db_connection_string
        reg = DbConnPoolRegistry()
        try:
            result = await reg.validate_and_register(conn_str, ["orders", "catalog", "ghost"])
            assert result.registered == ["orders", "catalog"]
            assert result.missing == ["ghost"]
            assert reg.mode == "multi"
        finally:
            await reg.close_all()

    async def test_per_database_routing(self, multi_db_connection_string):
        conn_str, _ = multi_db_connection_string
        reg = DbConnPoolRegistry()
        try:
            await reg.validate_and_register(conn_str, ["orders", "catalog"])
            for dbname in ("orders", "catalog"):
                pool = await reg.get_pool(dbname)
                driver = SqlDriver(conn=pool)
                rows = await driver.execute_query("SELECT current_database() AS db")
                assert rows is not None
                assert rows[0].cells["db"] == dbname
        finally:
            await reg.close_all()

    async def test_lazy_open(self, multi_db_connection_string):
        conn_str, _ = multi_db_connection_string
        reg = DbConnPoolRegistry()
        try:
            await reg.validate_and_register(conn_str, ["orders", "catalog"])
            catalog_pool = reg._pools[(DEFAULT_ENV, "catalog")]
            assert catalog_pool.is_valid is False  # not opened by registration
            await reg.get_pool("catalog")
            assert catalog_pool.is_valid is True  # opened on first access
        finally:
            await reg.close_all()

    async def test_list_databases_tool(self, multi_db_connection_string):
        conn_str, _ = multi_db_connection_string
        reg = DbConnPoolRegistry()
        try:
            await reg.validate_and_register(conn_str, ["orders", "catalog"])
            with patch("postgres_mcp.server.db_registry", reg):
                result = await server.list_databases()
            assert result == {"databases": ["orders", "catalog"], "mode": "multi"}
        finally:
            await reg.close_all()

    async def test_unknown_database_friendly_error(self, multi_db_connection_string):
        conn_str, _ = multi_db_connection_string
        reg = DbConnPoolRegistry()
        try:
            await reg.validate_and_register(conn_str, ["orders", "catalog"])
            with (
                patch("postgres_mcp.server.db_registry", reg),
                patch("postgres_mcp.server.current_access_mode", AccessMode.UNRESTRICTED),
            ):
                response = await server.list_schemas(database_name="nonexistent")
            text = response[0].text
            assert "Error" in text
            assert "Unknown database 'nonexistent'" in text
            assert "Available databases: orders, catalog" in text
        finally:
            await reg.close_all()

    async def test_none_database_name_friendly_error_multi(self, multi_db_connection_string):
        conn_str, _ = multi_db_connection_string
        reg = DbConnPoolRegistry()
        try:
            await reg.validate_and_register(conn_str, ["orders", "catalog"])
            with (
                patch("postgres_mcp.server.db_registry", reg),
                patch("postgres_mcp.server.current_access_mode", AccessMode.UNRESTRICTED),
            ):
                response = await server.list_schemas(database_name=None)
            text = response[0].text
            assert "Error" in text
            assert "database_name is required" in text
            assert "Available databases: orders, catalog" in text
        finally:
            await reg.close_all()

    async def test_single_db_regression(self, multi_db_connection_string):
        conn_str, _ = multi_db_connection_string
        reg = DbConnPoolRegistry()
        try:
            result = await reg.validate_and_register(conn_str, None)
            assert reg.mode == "single"
            assert reg.get_names() == ["test_db"]
            assert result.registered == ["test_db"]
            with (
                patch("postgres_mcp.server.db_registry", reg),
                patch("postgres_mcp.server.current_access_mode", AccessMode.UNRESTRICTED),
            ):
                driver = await server.get_sql_driver(None)
                rows = await driver.execute_query("SELECT 1 AS x")
            assert rows is not None
            assert rows[0].cells["x"] == 1
        finally:
            await reg.close_all()
