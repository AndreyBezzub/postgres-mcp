import json
import sys
from unittest.mock import AsyncMock

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
