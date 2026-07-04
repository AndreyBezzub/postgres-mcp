"""Docker-backed integration tests for the multi-environment (``run_multi``) path.

Two local Postgres containers stand in for two environments — never a real
Aiven/HootCore host, never a VPN. One container is *paused* mid-test (pause, not
stop: a stopped container is re-assigned a new random host port on restart, which
would invalidate the DSN the registry already holds; pause keeps the port stable)
to prove:

* non-fatal startup — an unreachable environment is recorded in the availability
  map and never aborts the server;
* reconnect recovery — once the environment comes back, ``reconnect_all`` re-probes
  and restores it with no server restart.

Containers are created via ``tests/utils.create_postgres_container`` (the raw
``docker`` SDK the rest of the suite uses) — no ``testcontainers`` dependency, and
the DSN is bound to 127.0.0.1 by that helper (not "localhost", which resolves to
IPv6 first on Windows and hangs psycopg's async pool).
"""

import time
from unittest.mock import patch
from urllib.parse import urlparse

import docker
import psycopg
import pytest
from utils import create_postgres_container

import postgres_mcp.server as server
from postgres_mcp.server import AccessMode
from postgres_mcp.sql.db_conn_pool_registry import DbConnPoolRegistry
from postgres_mcp.sql.sql_driver import SqlDriver

TWO_ENV_PG_VERSION = "postgres:16"
EXTRA_DB = "orders"
ENV_A = "envA"
ENV_B = "envB"
ALL_DBS = ("test_db", EXTRA_DB)


def _container_for_dsn(dsn: str):
    """Find the running test container that publishes the DSN's host port."""
    port = str(urlparse(dsn).port)
    client = docker.from_env()
    for ct in client.containers.list(all=True):
        bindings = (ct.attrs.get("NetworkSettings", {}).get("Ports") or {}).get("5432/tcp") or []
        if any(b.get("HostPort") == port for b in bindings):
            return ct
    raise RuntimeError(f"No test container found publishing port {port}")


@pytest.fixture(scope="class")
def two_env_connection_strings():
    """Spin up TWO Postgres containers (two environments), each with test_db + orders."""
    gen_a = create_postgres_container(TWO_ENV_PG_VERSION)
    gen_b = create_postgres_container(TWO_ENV_PG_VERSION)
    dsn_a, _ = next(gen_a)
    dsn_b, _ = next(gen_b)
    for dsn in (dsn_a, dsn_b):
        with psycopg.connect(dsn, autocommit=True) as conn:  # autocommit: CREATE DATABASE
            conn.execute(f'CREATE DATABASE "{EXTRA_DB}"')
    try:
        yield dsn_a, dsn_b
    finally:
        for gen in (gen_a, gen_b):
            try:
                next(gen)  # drive the generator's cleanup (stop + remove)
            except StopIteration:
                pass


async def _register_two(reg: DbConnPoolRegistry, dsn_a: str, dsn_b: str, dbs=ALL_DBS):
    return await reg.register_environments(
        {
            ENV_A: {"base_dsn": dsn_a, "databases": list(dbs)},
            ENV_B: {"base_dsn": dsn_b, "databases": list(dbs)},
        }
    )


@pytest.mark.asyncio
class TestMultiEnvironment:
    async def test_non_fatal_startup_and_reconnect_recovery(self, two_env_connection_strings):
        """envB unreachable at startup -> non-fatal; envA usable; reconnect brings envB back."""
        dsn_a, dsn_b = two_env_connection_strings
        reg = DbConnPoolRegistry()
        ct_b = _container_for_dsn(dsn_b)
        try:
            ct_b.pause()  # envB goes dark before registration
            amap = await _register_two(reg, dsn_a, dsn_b)

            # Registration returned normally (no sys.exit / exception): the server "started".
            assert amap[ENV_A]["reachable"] is True
            assert set(amap[ENV_A]["dbs_ok"]) == set(ALL_DBS)
            assert amap[ENV_B]["reachable"] is False
            assert amap[ENV_B]["error"]  # a reason is recorded for the down env
            assert reg.get_names_for_env(ENV_B) == []

            # envA is fully queryable while envB is down.
            driver_a = SqlDriver(conn=await reg.get_pool("test_db", ENV_A))
            rows_a = await driver_a.execute_query("SELECT current_database() AS db")
            assert rows_a[0].cells["db"] == "test_db"

            # envB recovers; reconnect re-probes the unreachable env and restores it (no
            # restart; healthy envA is left untouched).
            ct_b.unpause()
            time.sleep(2)  # let PostgreSQL resume accepting connections
            amap2 = await reg.reconnect_all()
            assert amap2[ENV_A]["reachable"] is True
            assert amap2[ENV_B]["reachable"] is True
            assert set(amap2[ENV_B]["dbs_ok"]) == set(ALL_DBS)

            # And the recovered env is now actually usable.
            driver_b = SqlDriver(conn=await reg.get_pool(EXTRA_DB, ENV_B))
            rows_b = await driver_b.execute_query("SELECT current_database() AS db")
            assert rows_b[0].cells["db"] == EXTRA_DB
        finally:
            try:
                ct_b.reload()
                if ct_b.status == "paused":
                    ct_b.unpause()
            except Exception:
                pass
            await reg.close_all()

    async def test_list_databases_returns_global_availability_map(self, two_env_connection_strings):
        """list_databases is a global, side-effect-free availability surface (no environment arg)."""
        dsn_a, dsn_b = two_env_connection_strings
        reg = DbConnPoolRegistry()
        try:
            await _register_two(reg, dsn_a, dsn_b)
            with patch("postgres_mcp.server.db_registry", reg):
                result = await server.list_databases()
            assert result["mode"] == "multi-env"
            assert set(result["environments"].keys()) == {ENV_A, ENV_B}
            for env in (ENV_A, ENV_B):
                assert result["environments"][env]["reachable"] is True
                assert set(result["environments"][env]["dbs_ok"]) == set(ALL_DBS)
        finally:
            await reg.close_all()

    async def test_read_only_guard_rejects_writes_on_every_env(self, two_env_connection_strings):
        """Restricted mode rejects writes uniformly across every (environment, database) pool.

        Proven behaviorally: the write returns an error AND no table is created. A
        control assertion confirms the rejection is the access-mode guard (not a bad
        statement) — the identical DDL succeeds under UNRESTRICTED, then is dropped.
        """
        dsn_a, dsn_b = two_env_connection_strings
        reg = DbConnPoolRegistry()
        pairs = [(env, db) for env in (ENV_A, ENV_B) for db in ALL_DBS]
        write_sql = "CREATE TABLE guard_probe (id int)"
        try:
            await _register_two(reg, dsn_a, dsn_b)

            # Every (env, db) pool refuses the write in restricted mode.
            for env, db in pairs:
                with (
                    patch("postgres_mcp.server.db_registry", reg),
                    patch("postgres_mcp.server.current_access_mode", AccessMode.RESTRICTED),
                ):
                    resp = await server.execute_sql(environment=env, database_name=db, sql=write_sql)
                text = resp[0].text
                assert text.startswith("Error"), f"write unexpectedly allowed on ({env}, {db}): {text}"

            # The write must not have taken effect on ANY (env, db) pool.
            for env, db in pairs:
                driver = SqlDriver(conn=await reg.get_pool(db, env))
                rows = await driver.execute_query(
                    "SELECT count(*) AS n FROM information_schema.tables WHERE table_name = 'guard_probe'"
                )
                assert rows[0].cells["n"] == 0, f"guard_probe leaked into ({env}, {db})"

            # Control: the same DDL is accepted under UNRESTRICTED -> the refusal above
            # is the read-only access guard, not a malformed statement.
            with (
                patch("postgres_mcp.server.db_registry", reg),
                patch("postgres_mcp.server.current_access_mode", AccessMode.UNRESTRICTED),
            ):
                ok = await server.execute_sql(environment=ENV_A, database_name="test_db", sql=write_sql)
            assert not ok[0].text.startswith("Error"), f"control write failed: {ok[0].text}"
            drop_driver = SqlDriver(conn=await reg.get_pool("test_db", ENV_A))
            await drop_driver.execute_query("DROP TABLE guard_probe")
        finally:
            await reg.close_all()


@pytest.mark.asyncio
class TestMultiEnvironmentMasking:
    """Password masking on the availability map — real DbConnPool, no container needed.

    Both cases point at a dead local endpoint / malformed DSN (never a real host or
    VPN); the assertion is that the recorded error never exposes the password.
    """

    async def test_connect_failure_error_is_masked(self):
        secret = "supersecret_pw"
        # 127.0.0.1:1 is closed -> fast connection refusal; never a real host / VPN.
        dsn = f"postgresql://postgres:{secret}@127.0.0.1:1/test_db"
        reg = DbConnPoolRegistry()
        try:
            amap = await reg.register_environments({"envX": {"base_dsn": dsn, "databases": ["test_db"]}})
        finally:
            await reg.close_all()
        assert amap["envX"]["reachable"] is False
        assert secret not in (amap["envX"]["error"] or "")

    async def test_malformed_dsn_error_is_masked(self):
        secret = "supersecret_pw"
        # Invalid port token -> psycopg rejects the DSN at parse time (no network).
        dsn = f"postgresql://postgres:{secret}@127.0.0.1:notaport/test_db"
        reg = DbConnPoolRegistry()
        try:
            amap = await reg.register_environments({"envY": {"base_dsn": dsn, "databases": ["test_db"]}})
        finally:
            await reg.close_all()
        assert amap["envY"]["reachable"] is False
        assert secret not in (amap["envY"]["error"] or "")
