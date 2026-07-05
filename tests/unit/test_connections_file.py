import json
import sys
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest

from postgres_mcp import server

VALID_MAP = {
    "prod": {
        "base_dsn": "postgresql://user:pw@prod-host:5432/postgres",
        "databases": ["orders", "catalog"],
    },
    "uat2": {
        "base_dsn": "postgresql://user:pw@uat2-host:5432/postgres",
        "databases": ["orders"],
    },
}


def test_load_valid(tmp_path):
    """A valid multi-env JSON file loads back into an equal dict."""
    path = tmp_path / "envs.json"
    path.write_text(json.dumps(VALID_MAP), encoding="utf-8")

    result = server.load_connections_file(str(path))

    assert result == VALID_MAP


def test_load_missing_file(tmp_path):
    """A nonexistent path exits(1)."""
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(SystemExit):
        server.load_connections_file(str(missing))


def test_load_invalid_json(tmp_path):
    """Malformed JSON exits(1)."""
    path = tmp_path / "bad.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        server.load_connections_file(str(path))


def test_load_non_object(tmp_path):
    """A JSON array and an empty object both exit(1) (top level must be a non-empty object)."""
    array_path = tmp_path / "array.json"
    array_path.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit):
        server.load_connections_file(str(array_path))

    empty_path = tmp_path / "empty.json"
    empty_path.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        server.load_connections_file(str(empty_path))


@pytest.mark.asyncio
async def test_main_routes_to_run_multi(monkeypatch, tmp_path):
    """--connections-file routes through run_multi with the parsed map and returns cleanly."""
    path = tmp_path / "envs.json"
    path.write_text(json.dumps(VALID_MAP), encoding="utf-8")

    stub = AsyncMock()
    monkeypatch.setattr(server, "run_multi", stub)
    monkeypatch.setattr(sys, "argv", ["postgres-mcp", "--connections-file", str(path)])

    # Must return without raising the missing-DATABASE_URI ValueError from the single-host path.
    await server.main()

    stub.assert_awaited_once()
    assert stub.await_args is not None
    assert stub.await_args.args[0] == VALID_MAP


@pytest.mark.asyncio
async def test_main_rejects_file_plus_databases(monkeypatch, tmp_path):
    """--connections-file together with --databases exits (argparse parser.error) and never calls run_multi."""
    path = tmp_path / "envs.json"
    path.write_text(json.dumps(VALID_MAP), encoding="utf-8")

    stub = AsyncMock()
    monkeypatch.setattr(server, "run_multi", stub)
    monkeypatch.setattr(
        sys,
        "argv",
        ["postgres-mcp", "--connections-file", str(path), "--databases", "x"],
    )

    with pytest.raises(SystemExit):
        await server.main()

    stub.assert_not_awaited()


# --- resolve_connections: schema resolution (inline + structured + password sources) -------------


def test_resolve_inline_passthrough():
    """A raw inline map resolves to an equal map (inline entries pass through unchanged)."""
    assert server.resolve_connections(VALID_MAP) == VALID_MAP


def test_resolve_reserved_key_skipped():
    """A top-level `_`-prefixed key is skipped; a sibling real env still resolves."""
    data = {
        "_settings": {"VaultAddr": "https://vault.example"},
        "prod": {
            "base_dsn": "postgresql://user:pw@prod-host:5432/postgres",
            "databases": ["orders"],
        },
    }
    resolved = server.resolve_connections(data)
    assert "_settings" not in resolved
    assert resolved["prod"] == {
        "base_dsn": "postgresql://user:pw@prod-host:5432/postgres",
        "databases": ["orders"],
    }


def test_resolve_structured_env(monkeypatch):
    """A structured entry with password={"env": VAR} assembles a percent-encoded DSN; databases kept."""
    monkeypatch.setenv("PGPASS_X", "p@ss:w0rd")
    data = {
        "prod": {
            "host": "db-host",
            "port": 5432,
            "user": "mcp_user",
            "dbname": "postgres",
            "sslmode": "require",
            "password": {"env": "PGPASS_X"},
            "databases": ["orders", "catalog"],
        },
    }
    resolved = server.resolve_connections(data)
    expected_dsn = f"postgresql://mcp_user:{quote('p@ss:w0rd', safe='')}@db-host:5432/postgres?sslmode=require"
    assert resolved["prod"]["base_dsn"] == expected_dsn
    assert resolved["prod"]["databases"] == ["orders", "catalog"]


def test_resolve_structured_file(tmp_path):
    """A password={"file": path} reads the file and strips the trailing newline before encoding."""
    secret = tmp_path / "pw.txt"
    secret.write_text("s3cr3t\n", encoding="utf-8")
    data = {
        "prod": {
            "host": "db-host",
            "user": "mcp_user",
            "dbname": "postgres",
            "password": {"file": str(secret)},
            "databases": ["orders"],
        },
    }
    resolved = server.resolve_connections(data)
    assert resolved["prod"]["base_dsn"] == "postgresql://mcp_user:s3cr3t@db-host/postgres"


def test_resolve_structured_keyring(monkeypatch):
    """A password={"keyring": {...}} resolves via keyring.get_password into the assembled DSN."""
    monkeypatch.setattr("keyring.get_password", lambda service, username: "kr-pass")
    data = {
        "prod": {
            "host": "db-host",
            "port": 5432,
            "user": "mcp_user",
            "dbname": "postgres",
            "password": {"keyring": {"service": "svc", "username": "u"}},
            "databases": ["orders"],
        },
    }
    resolved = server.resolve_connections(data)
    assert resolved["prod"]["base_dsn"] == "postgresql://mcp_user:kr-pass@db-host:5432/postgres"


def test_resolve_password_percent_encoding(monkeypatch):
    """Reserved/unicode chars in the password are percent-encoded (raw chars absent, escapes present)."""
    monkeypatch.setenv("PGPASS_ENC", "p@:/ é")
    data = {
        "prod": {
            "host": "db-host",
            "user": "mcp_user",
            "dbname": "postgres",
            "password": {"env": "PGPASS_ENC"},
            "databases": ["orders"],
        },
    }
    dsn = server.resolve_connections(data)["prod"]["base_dsn"]
    # The raw reserved chars must not leak into the userinfo.
    assert "p@:/ é" not in dsn
    for escape in ("%40", "%3A", "%2F", "%20", "%C3%A9"):
        assert escape in dsn


def test_resolve_per_env_failure_nonfatal():
    """A structured env missing `password` is recorded unreachable (base_dsn=None) without raising;
    a sibling valid env still resolves to a str base_dsn; databases are preserved on the bad env."""
    data = {
        "bad": {
            "host": "db-host",
            "user": "mcp_user",
            "dbname": "postgres",
            "databases": ["orders"],
        },
        "good": {
            "base_dsn": "postgresql://user:pw@good-host:5432/postgres",
            "databases": ["catalog"],
        },
    }
    resolved = server.resolve_connections(data)
    assert resolved["bad"]["base_dsn"] is None
    assert resolved["bad"]["databases"] == ["orders"]
    assert isinstance(resolved["good"]["base_dsn"], str)
    assert resolved["good"]["base_dsn"] == "postgresql://user:pw@good-host:5432/postgres"


def test_resolve_keyring_missing_extra(monkeypatch):
    """With the keyring module absent (ImportError on import), a keyring source is non-fatal:
    the env is marked unreachable (base_dsn=None) rather than crashing the load."""
    monkeypatch.setitem(sys.modules, "keyring", None)
    data = {
        "prod": {
            "host": "db-host",
            "user": "mcp_user",
            "dbname": "postgres",
            "password": {"keyring": {"service": "svc", "username": "u"}},
            "databases": ["orders"],
        },
    }
    resolved = server.resolve_connections(data)
    assert resolved["prod"]["base_dsn"] is None
    assert resolved["prod"]["databases"] == ["orders"]


def test_resolve_secret_resolver_hook():
    """An unrecognised source dict defers to secret_resolver when provided; without it, non-fatal."""
    data = {
        "prod": {
            "host": "db-host",
            "user": "mcp_user",
            "dbname": "postgres",
            "password": {"custom": "x"},
            "databases": ["orders"],
        },
    }
    with_hook = server.resolve_connections(data, secret_resolver=lambda src, env: "pw")
    assert with_hook["prod"]["base_dsn"] == "postgresql://mcp_user:pw@db-host/postgres"

    without_hook = server.resolve_connections(data)
    assert without_hook["prod"]["base_dsn"] is None
