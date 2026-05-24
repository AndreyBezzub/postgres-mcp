# Multi-Database Support — Implementation Notes

> Source of truth: the Step-1 brief. These notes encode the fixed decisions verbatim and
> map them onto the *actual* code at commit `v0.3.0-12-g07eb329`. Line numbers below are
> from the current files, not the brief.

## 1. Architecture overview

**(a) Registry replaces the singleton.** Today `server.py:57` holds one global
`db_connection = DbConnPool()` bound to a single database. We replace it with a single
`db_registry = DbConnPoolRegistry()`. The registry owns a `name -> DbConnPool` map, one
`DbConnPool` per validated database. All credentials/host come from the one `DATABASE_URI`;
each per-database `DbConnPool` is built by swapping only the dbname (path) of that URI.
Every tool gains a `database_name` parameter and resolves its driver through
`get_sql_driver(database_name)`, which fetches the right pool from the registry.

**(b) Lazy lifecycle + validation flow.** At startup, if `--databases=db1,db2,...` is given
(multi-DB mode), the registry connects once to a *discovery DB* (the dbname from
`DATABASE_URI`, or `postgres` if the URI has no path), runs the `pg_database` validation
query, and registers only the validated names — storing `DbConnPool` instances **without
opening them** (`open=False`, per `DbConnPool.__init__`). The first tool call against a DB
triggers `pool.pool_connect()`, which opens the pool. Without `--databases` (single-DB mode)
the registry registers exactly one DB (the discovery dbname) and runs no validation query,
preserving existing behavior functionally (the `database_name` parameter defaults to that
sole DB when the caller omits it).

## 2. Phase 1 — DbConnPoolRegistry

**File:** `src/postgres_mcp/sql/db_conn_pool_registry.py` (new)

Follows local conventions: `typing.Optional/List/Dict`, single-line imports, module logger,
`DbConnPool` reused unchanged from `sql_driver.py`. URL rewriting uses `urllib.parse`
(`urlparse`/`urlunparse`), the same primitives already imported in `sql_driver.py`.

```python
"""Registry of per-database connection pools sharing one set of credentials."""

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Dict
from typing import List
from typing import Optional
from urllib.parse import urlparse
from urllib.parse import urlunparse

from .sql_driver import DbConnPool

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    registered: List[str]
    missing: List[str]
    disallowed: List[str] = field(default_factory=list)


class DatabaseValidationError(Exception):
    """Raised when a tool targets an unknown / missing database_name."""

    def __init__(self, message: str, available_databases: Optional[List[str]] = None):
        super().__init__(message)
        self.available_databases = available_databases or []


class DbConnPoolRegistry:
    """Holds one DbConnPool per validated database on a single PG server."""

    def __init__(self) -> None:
        self._pools: Dict[str, DbConnPool] = {}
        self._base_url: Optional[str] = None
        self._discovery_db: Optional[str] = None
        self._mode: str = "single"  # "single" | "multi"

    @property
    def mode(self) -> str:
        """Return "single" or "multi"."""
        return self._mode

    def get_names(self) -> List[str]:
        """Names of all registered databases, in registration order."""
        return list(self._pools.keys())

    def is_registered(self, name: str) -> bool:
        """True if name is a registered database."""
        return name in self._pools

    async def validate_and_register(
        self, base_url: str, database_names: Optional[List[str]]
    ) -> ValidationResult:
        """Validate requested DBs against pg_database and register pools (lazy, open=False)."""
        ...  # pseudo-code below

    async def get_pool(self, database_name: Optional[str]) -> DbConnPool:
        """Return the (lazily opened) pool for database_name, else raise DatabaseValidationError."""
        ...

    async def close_all(self) -> None:
        """Close every registered pool (used on shutdown)."""
        for pool in self._pools.values():
            await pool.close()

    def _build_db_url(self, dbname: str) -> str:
        parsed = urlparse(self._base_url)
        return urlunparse(parsed._replace(path=f"/{dbname}"))

    def _discovery_dbname(self, base_url: str) -> str:
        parsed = urlparse(base_url)
        return parsed.path.lstrip("/") or "postgres"
```

**`validate_and_register()` pseudo-code** (encodes fixed decision #3 exactly):

```python
self._base_url = base_url
self._discovery_db = self._discovery_dbname(base_url)

if not database_names:                       # decision #2: single-DB mode
    self._mode = "single"
    name = self._discovery_db
    self._pools[name] = DbConnPool(self._build_db_url(name))   # open=False, lazy
    return ValidationResult(registered=[name], missing=[], disallowed=[])

self._mode = "multi"
discovery = DbConnPool(self._build_db_url(self._discovery_db))
try:
    pool = await discovery.pool_connect()    # opens discovery pool only
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT datname FROM pg_database "
                "WHERE datname = ANY(%s) AND datallowconn = true",
                [database_names],
            )
            valid = {row[0] for row in await cur.fetchall()}
finally:
    await discovery.close()                  # discovery pool not retained

registered = [n for n in database_names if n in valid]
missing = [n for n in database_names if n not in valid]
for name in registered:                      # lazy: store, do not open
    self._pools[name] = DbConnPool(self._build_db_url(name))
return ValidationResult(registered=registered, missing=missing, disallowed=[])
```

**`get_pool()` lazy-open:**

```python
pool = self._pools.get(database_name) if database_name else None
if pool is None:
    raise DatabaseValidationError(
        f"database_name is required. Available databases: {', '.join(self.get_names())}. "
        f"Call list_databases for the current list.",
        available_databases=self.get_names(),
    )
await pool.pool_connect()   # idempotent: returns existing pool if already valid, else opens
return pool
```

**Exports to add in `src/postgres_mcp/sql/__init__.py`** (import lines + `__all__`):
`DbConnPoolRegistry`, `DatabaseValidationError`, `ValidationResult`.

## 3. Phase 2 — server.py refactoring

All line ranges are from the current `server.py` (694 lines).

| Where | Current | Change |
|-------|---------|--------|
| line 30-33 imports | `from .sql import DbConnPool ...` | add `from .sql import DbConnPoolRegistry`, `from .sql import DatabaseValidationError` |
| line 57 | `db_connection = DbConnPool()` | `db_registry = DbConnPoolRegistry()` |
| lines 62-71 | `async def get_sql_driver() -> Union[...]` | new signature `async def get_sql_driver(database_name: Optional[str]) -> Union[SqlDriver, SafeSqlDriver]` (see below) |
| line 638 (`main`) | `await db_connection.pool_connect(database_url)` | replaced by `await db_registry.validate_and_register(...)` (Phase 3) |
| line 688 (`shutdown`) | `await db_connection.close()` | `await db_registry.close_all()` |

**New `get_sql_driver` body** (resolves pool, applies single-mode default, keeps the
RESTRICTED/UNRESTRICTED branching identical):

```python
async def get_sql_driver(database_name: Optional[str]) -> Union[SqlDriver, SafeSqlDriver]:
    """Get the SQL driver for a specific database, honoring the current access mode."""
    if database_name is None and db_registry.mode == "single":
        database_name = db_registry.get_names()[0]   # backward-compatible default
    pool = await db_registry.get_pool(database_name)  # raises DatabaseValidationError if None/unknown
    base_driver = SqlDriver(conn=pool)
    if current_access_mode == AccessMode.RESTRICTED:
        return SafeSqlDriver(sql_driver=base_driver, timeout=30)
    return base_driver
```

Add `from typing import Optional` to the existing typing imports (block at lines 9-12).

### Per-tool modifications

For every tool, (1) prepend a `database_name` parameter and (2) change the existing
`await get_sql_driver()` call to `await get_sql_driver(database_name)`. The validation lives
inside `get_sql_driver` → on `None`/unknown it raises `DatabaseValidationError`, whose
`str()` is exactly: `database_name is required. Available databases: orders, catalog, users.
Call list_databases for the current list.` The tool's existing `except Exception as e:
return format_error_response(str(e))` turns that into the user-facing message (decision #4).

The exact parameter declaration to add as the **first** parameter of each tool:

```python
database_name: Optional[str] = Field(None, description=DATABASE_NAME_PARAM_DESC),
```

`DATABASE_NAME_PARAM_DESC` is a module-level placeholder string set at import time and
**overwritten in `main()`** after validation with the live DB list (see Phase 3 dynamic
injection). Define near the constants (around line 41):

```python
DATABASE_NAME_PARAM_DESC = "Target database name. Call list_databases for available names."
```

| # | Tool | Lines | Has try/except? | Insert point for `get_sql_driver(database_name)` |
|---|------|-------|-----------------|-----------------------------------|
| 1 | `list_schemas` | 84-113 | yes (93) | line 94 |
| 2 | `list_objects` | 116-187 | yes (128) | line 129 |
| 3 | `get_object_details` | 190-326 | yes (203) | line 204 |
| 4 | `explain_query` | 329-411 | yes (365) | line 366 |
| 5 | `execute_sql` | 414-427 (registered in `main`) | yes (419) | line 420 |
| 6 | `analyze_workload_indexes` | 430-454 (`@validate_call`) | yes (443) | line 444 |
| 7 | `analyze_query_indexes` | 457-487 (`@validate_call`) | yes (476) | line 477 |
| 8 | `analyze_db_health` | 490-520 | **NO try/except** | wrap body in try/except (see note) |
| 9 | `get_top_queries` | 523-554 | yes (539) | line 540 |

> **Tool count clarification.** 8 tools are statically decorated with `@mcp.tool`;
> `execute_sql` (lines 414-427) is registered dynamically in `main()` via `mcp.add_tool`
> (lines 607-624). Total tool surface = **9** (matches the README API table). `list_databases`
> (below) makes it **10** after this work.

> **`analyze_db_health` (decision #4 edge):** it currently has no try/except, so a raised
> `DatabaseValidationError` would escape as a protocol error instead of a friendly message.
> Wrap its body:
> ```python
> async def analyze_db_health(database_name: Optional[str] = Field(None, description=DATABASE_NAME_PARAM_DESC),
>                             health_type: str = Field(...)) -> ResponseType:
>     try:
>         sql_driver = await get_sql_driver(database_name)
>         health_tool = DatabaseHealthTool(sql_driver)
>         result = await health_tool.health(health_type=health_type)
>         return format_text_response(result)
>     except Exception as e:
>         logger.error(f"Error analyzing database health: {e}")
>         return format_error_response(str(e))
> ```

> **Exception — `@validate_call` parameter order (tools 6 & 7).** For `analyze_workload_indexes`
> and `analyze_query_indexes`, `database_name` is placed **last** (not first) because pydantic's
> `@validate_call` rejects an optional parameter preceding a required one (`queries`). The other
> 7 tools keep it first per the table above. MCP passes arguments by name, so caller behavior is
> identical.

### New tool: `list_databases`

```python
@mcp.tool(
    description="List the databases this server is configured to access.",
    annotations=ToolAnnotations(
        title="List Databases",
        readOnlyHint=True,
    ),
)
async def list_databases() -> dict:
    """Return the registered database names and the server mode."""
    return {"databases": db_registry.get_names(), "mode": db_registry.mode}
```

> Note: this returns a `dict` per decision #5 (FastMCP serializes it as structured content).
> Other tools return `ResponseType` via `format_text_response`; this minor return-type
> divergence is intentional and called out in §7.

### Dynamic `database_name` description (generated after validation)

The DB list is known only at runtime, so in `main()` (after `validate_and_register`) build
the live description and overwrite both the module global and each registered tool's
parameter schema:

```python
names = db_registry.get_names()
desc = (f"Target database. Available: {', '.join(names)}. "
        f"Required in multi-DB mode; call list_databases for the current list.")
globals()["DATABASE_NAME_PARAM_DESC"] = desc
_inject_database_name_description(desc)   # patches each tool's input schema
```

The patch helper walks the FastMCP tool registry and rewrites the `database_name`
property description in each tool's JSON input schema. The exact API path
(`mcp._tool_manager.list_tools()` → mutate `tool.parameters["properties"]["database_name"]`)
is verified on mcp 1.27.1; full implementation in §4.

## 4. Phase 3 — main() bootstrap

**File:** `src/postgres_mcp/server.py`, `main()` (lines 557-669).

**New CLI argument** (add to the argparse block, e.g. after the `--access-mode` arg ~line 567):

```python
parser.add_argument(
    "--databases",
    type=str,
    default=None,
    help="Comma-separated database names on the same PG server to expose (multi-DB mode). "
         "If omitted, single-DB mode uses the dbname from DATABASE_URI.",
)
```

**Parse:**

```python
database_names = (
    [d.strip() for d in args.databases.split(",") if d.strip()]
    if args.databases else None
)
```

**Branching** (replaces the `await db_connection.pool_connect(...)` try/except at lines 636-646;
runs after `database_url` is resolved at line 629):

```python
global db_registry
result = await db_registry.validate_and_register(database_url, database_names)

if database_names and len(result.registered) == 0:
    logger.error("None of the requested databases are available: %s",
                 ", ".join(result.missing))
    sys.exit(1)                                  # decision #3: exit(1) when nothing valid
if result.missing:
    logger.warning("Skipping databases not found / not connectable: %s",
                   ", ".join(result.missing))    # decision #3: warn + continue
logger.info("Registered %d database(s) in %s mode: %s",
            len(result.registered), db_registry.mode, ", ".join(result.registered))

# dynamic database_name description injection (Phase 2)
names = db_registry.get_names()
desc = (f"Target database. Available: {', '.join(names)}. "
        f"Required in multi-DB mode; call list_databases for the current list.")
globals()["DATABASE_NAME_PARAM_DESC"] = desc
_inject_database_name_description(desc)
```

**Single-DB path:** `database_names is None` → `validate_and_register` registers the
discovery dbname only, `mode == "single"`, no `pg_database` query, no `exit(1)` branch.

**`exit(1)` behavior:** only in multi-DB mode and only when `len(result.registered) == 0`.
Partial validation (`0 < registered < provided`) logs a warning and continues.

**Dynamic tool-description injection — verified mechanism.** FastMCP keeps tools in
`mcp._tool_manager._tools` (a name→Tool dict). Each `Tool.parameters` is a mutable JSON-schema
dict; FastMCP's `list_tools` builds the protocol-level `inputSchema` by passing this dict by
reference each call (`MCPTool(..., inputSchema=info.parameters, ...)`), so mutation propagates
live with no rebuild needed. Verified on mcp 1.27.1 (satisfies pyproject constraint
`mcp[cli]>=1.25.0`); the relevant `ToolManager.list_tools()` body is just
`return list(self._tools.values())`, stable across 1.25–1.27. Confirmed working for tools
registered via `@mcp.tool`, `mcp.add_tool`, and `@validate_call`-wrapped tools.

```python
def _inject_database_name_description(desc: str) -> None:
    """Patch the database_name parameter description on every registered tool.

    Mutates tool.parameters in place; FastMCP reads this dict by reference when
    serving protocol-level list_tools, so the change is visible immediately.
    Verified on mcp >=1.25.0.
    """
    for tool in mcp._tool_manager.list_tools():
        props = tool.parameters.get("properties", {})
        if "database_name" in props:
            props["database_name"]["description"] = desc
```

`shutdown()` (lines 672-694): change line 688 `await db_connection.close()` →
`await db_registry.close_all()`.

## 5. Phase 4 — Tests

Conventions confirmed from the repo: `@pytest.mark.asyncio` (pytest-asyncio, **not anyio**;
`asyncio_default_fixture_loop_scope="function"` in `pyproject.toml`), mocks via
`unittest.mock` (`AsyncMock`/`MagicMock`/`patch`) — see `tests/unit/sql/test_db_conn_pool.py`.
The container fixture is **`test_postgres_connection_string`** (in `tests/conftest.py`,
`scope="class"`, parametrized over `postgres:12/15/16`), yielding a tuple
`(connection_string, version)` from `create_postgres_container` in `tests/utils.py`. It
creates exactly one DB, `test_db`.

### `tests/unit/sql/test_db_conn_pool_registry.py` (new, unit, mocked)

- `_build_db_url` swaps the dbname/path while keeping creds+host (`.../test_db` → `.../orders`)
- `_discovery_dbname`: empty/no path → `"postgres"`; `/foo` → `"foo"`
- single-DB mode (`database_names=None`): registers exactly the discovery DB, `mode=="single"`,
  no `pg_database` query issued
- multi-DB mode: patch the discovery `DbConnPool`/cursor to return a row set; assert
  `registered`/`missing` split is correct
- partial validation: requested `[orders, ghost]`, DB returns only `orders` →
  `registered==[orders]`, `missing==[ghost]`
- all-invalid: requested all-missing → `registered==[]` (drives `main()` exit(1), tested at
  bootstrap level)
- lazy open: `get_pool(name)` calls `pool.pool_connect()` exactly once; registration alone
  does **not** open
- unknown / `None` database: `get_pool` raises `DatabaseValidationError`; assert message text
  and `.available_databases`
- `close_all` calls `.close()` on every registered pool
- single-mode default: `get_sql_driver(None)` resolves to the sole DB (test via registry +
  patched `get_pool`)

### `tests/integration/test_multi_database.py` (new, Docker-backed)

**Fixture extension — concrete.** Add a helper in `tests/utils.py` and a fixture in
`tests/conftest.py` that builds extra DBs *after* the container is ready. `CREATE DATABASE`
cannot run inside a transaction, so use an autocommit connection:

```python
# tests/conftest.py
@pytest.fixture(scope="class")
def multi_db_connection_string(test_postgres_connection_string):
    conn_str, version = test_postgres_connection_string         # base points at test_db
    import psycopg
    with psycopg.connect(conn_str, autocommit=True) as conn:    # autocommit: CREATE DATABASE
        for name in ("orders", "catalog"):
            conn.execute(f'CREATE DATABASE "{name}"')
    yield conn_str, version
```

- Required: Docker available (fixture `pytest.skip`s otherwise — same as existing); no extra
  env vars (the fixture builds the URI). `DATABASE_URI` is *not* required for these tests.
- Discovery DB = `test_db` (the dbname embedded in the base connection string).

**Test cases:**
- `validate_and_register(base, ["orders","catalog","ghost"])` → `registered==["orders","catalog"]`,
  `missing==["ghost"]`, `mode=="multi"`
- driver against `orders`: `SELECT current_database()` returns `orders`; against `catalog`
  returns `catalog` (proves per-DB routing via URL rewrite)
- lazy open: pool for `catalog` reports not-valid until first query, then valid
- `list_databases()` → `{"databases": ["orders","catalog"], "mode": "multi"}`
- unknown `database_name` through a tool → friendly `format_error_response` text
- **single-DB regression:** `validate_and_register(base, None)` → `mode=="single"`, one DB
  (`test_db`), `get_sql_driver(None)` runs a query successfully

## 6. Phase 5 — Documentation

**README.md:** insert a new top-level section **`## Multi-database mode`** immediately after
the `#### Other MCP Clients` block (ends line 237) and before `## SSE Transport` (line 239).
Content: explain that one server can serve several DBs on the same host sharing one
`DATABASE_URI`, that the LLM passes `database_name` per call, and that `list_databases` lists
them. Example client config:

```json
{
  "mcpServers": {
    "postgres": {
      "command": "postgres-mcp",
      "args": [
        "--access-mode=unrestricted",
        "--databases=orders,catalog,users"
      ],
      "env": {
        "DATABASE_URI": "postgresql://username:password@localhost:5432/postgres"
      }
    }
  }
}
```

Note in the section that the dbname in `DATABASE_URI` is used as the *discovery* DB for
validation and is the single-DB fallback when `--databases` is omitted.

**CHANGELOG:** the repo has no `CHANGELOG.md`; the record of record is Conventional Commit
messages. If a changelog is added later, the entry would be:
`feat(server): add multi-database support via --databases`.

## 7. Risk log

1. **Verification #2 uses `mypy`, but the project uses `pyright`.** `pyproject.toml` pins
   `pyright==1.1.408` (dev group) and has a `[tool.pyright]` block; `mypy` is **not** a
   dependency and there is no mypy config. Running `python -m mypy src/postgres_mcp/` will
   fail (module not installed) unless mypy is added. The §8 block is copied verbatim as
   instructed — the /goal phase must either install mypy or substitute
   `python -m pyright src/postgres_mcp/`. **Flagged, not silently changed.**

2. **FastMCP dynamic parameter-description injection — RESOLVED pre-goal.** Tools are registered
   at import time via `@mcp.tool` (plus `execute_sql` via `mcp.add_tool` in `main()`), but the
   `database_name` description must include the live DB list known only after `--databases` is
   parsed. Verified path on mcp 1.27.1: walk `mcp._tool_manager.list_tools()` and mutate
   `tool.parameters["properties"]["database_name"]["description"]` in place. FastMCP's
   `list_tools` builds protocol-level `inputSchema` from this dict by reference each call, so
   mutation propagates live with no rebuild. Confirmed working for `@mcp.tool`, `mcp.add_tool`,
   and `@validate_call`-wrapped tools. Full code in §4. No fallback needed.

3. **`_POSTGRES_VERSION` global cache is NOT keyed by database** (`sql/extension_utils.py:12-14`,
   with an explicit TODO). For THIS task it is **safe**: all databases live on the *same* PG
   server, so the server version is identical across them. It would become a real bug only
   under multi-server (explicitly out of scope). The HypoPG check
   (`check_hypopg_installation_status`) is queried live per call (no cache), so per-DB
   extension differences are handled correctly.

4. **`pg_stat_statements` is server-global, not per-database.** It is a `shared_preload_library`
   that tracks statements across *all* databases; the `pg_stat_statements` view has a `dbid`
   column. `get_top_queries` / `analyze_workload_indexes` connected to database X will surface
   queries from *all* databases unless filtered by `dbid = (current db oid)`. `TopQueriesCalc`
   is **not** modified (non-goal), so in multi-DB mode "top queries" remain server-wide
   regardless of `database_name`. Also: the view only exists in databases where
   `CREATE EXTENSION pg_stat_statements` has run — querying it from a DB without the extension
   fails (same as today). Document this as a known limitation in the README section.

5. **Decision #2 (byte-identical single-DB) vs decision #4 (`database_name` on every tool).**
   Adding an optional `database_name` to the input schema technically changes the tool *schema*.
   Reconciled by defaulting `database_name` to the sole registered DB in single mode
   (`get_sql_driver`), so single-DB callers can omit it and behavior is **functionally**
   unchanged. Verification #9 ("same tool surface") is interpreted functionally, not as a
   byte-identical JSON schema.

6. **Decision #6 (lazy pools) changes startup connection timing.** The original code eagerly
   called `db_connection.pool_connect(database_url)` at startup (line 638) and logged a warning
   on failure. With lazy pools, single-DB mode no longer connects at startup, so a bad
   connection surfaces on the first tool call instead of at boot. Multi-DB mode still connects
   once to the discovery DB during validation. Accepted consequence of decision #6.

7. **`ValidationResult.disallowed` cannot be populated by decision #3's exact query.** The
   pinned query (`... AND datallowconn = true`) folds both non-existent and connection-disabled
   DBs into `missing`. The `disallowed` field is kept for API completeness (and future use) but
   stays empty unless an *optional* secondary query
   (`SELECT datname FROM pg_database WHERE datname = ANY(%s) AND datallowconn = false`) is added
   to classify them. Decision #3's query is used as-is for `registered`.

8. **Connection budget.** Each `DbConnPool` uses `max_size=5` (`sql_driver.py:92`). N registered
   databases → up to 5·N server connections once all pools open. Lazy opening mitigates this
   (only touched DBs open). Worth a note for users registering many DBs; no code change needed.

9. **`psycopg_pool` conninfo is fixed at construction.** `DbConnPool.pool_connect` short-circuits
   when a valid pool exists and never re-points conninfo. This is fine for the registry design
   (one `DbConnPool` per dbname, each with its own URL) and is the reason we build separate
   instances rather than mutating one pool's conninfo.

10. **`@validate_call` on tools 6 & 7.** `analyze_workload_indexes` / `analyze_query_indexes`
    are wrapped with `@validate_call`. Adding `database_name: Optional[str] = Field(None, ...)`
    as a parameter should be accepted by pydantic, but verify the decorator handles the
    `Optional` default cleanly during /goal.

## 8. Acceptance checklist for /goal phase

> Copied verbatim from the brief. All command outputs must appear in the /goal transcript.

1. `python -m pytest tests/ -x --tb=short` exits 0 with full output visible
2. `python -m mypy src/postgres_mcp/` exits 0 with full output visible
3. `test -f src/postgres_mcp/sql/db_conn_pool_registry.py && wc -l src/postgres_mcp/sql/db_conn_pool_registry.py`
4. `test -f tests/integration/test_multi_database.py && wc -l tests/integration/test_multi_database.py`
5. `grep -q "## Multi-database mode" README.md && echo OK`
6. `python -m pytest tests/unit/sql/test_db_conn_pool.py tests/unit/test_access_mode.py -v` passes (single-DB regression)
7. `git branch --show-current` returns `feat/multi-database-support`
8. `git log --format=%s feat/multi-database-support ^main | head -5` — at least one commit matches `^(feat|test|docs|refactor)(\(.+\))?: .+`
9. The single-DB mode is preserved: starting the server without --databases and with DATABASE_URI=postgres://.../somedb produces the same tool surface as before (plus the new list_databases tool returning {mode: "single"})

> Note for the evaluator: criterion #2 (`mypy`) — see §7 item 1; the project ships `pyright`,
> not `mypy`. Either install `mypy` or run `python -m pyright src/postgres_mcp/` to satisfy the
> intent (clean type check).

## 9. Files inventory

**Created**
- `src/postgres_mcp/sql/db_conn_pool_registry.py` — registry, `ValidationResult`,
  `DatabaseValidationError`
- `tests/unit/sql/test_db_conn_pool_registry.py` — mocked unit tests
- `tests/integration/test_multi_database.py` — Docker-backed integration tests

**Modified**
- `src/postgres_mcp/server.py` — ~+95 / -15 lines: singleton→registry (l.57), `get_sql_driver`
  signature (l.62-71), `database_name` on 9 tools, `list_databases` tool, `--databases` arg +
  validation + dynamic description in `main()` (l.557-669), `shutdown` close_all (l.688),
  `Optional` import + `DATABASE_NAME_PARAM_DESC` constant
- `src/postgres_mcp/sql/__init__.py` — ~+6 lines: export registry symbols
- `tests/conftest.py` — `multi_db_connection_string` fixture (~+12 lines)
- `tests/utils.py` — optional CREATE DATABASE helper (~+8 lines, if not inlined in conftest)
- `README.md` — new `## Multi-database mode` section (~+30 lines), after line 237
- `tests/unit/test_access_mode.py` — updated patch paths from `db_connection` → `db_registry.get_pool`; functional behaviour unchanged
- `tests/unit/sql/test_readonly_enforcement.py` — updated patch paths from `db_connection` → `db_registry.get_pool`; functional behaviour unchanged
- `tests/unit/test_transport.py` — dropped `db_connection.pool_connect` mock (single-mode lazy init no longer connects at startup); functional behaviour unchanged

**Explicitly NOT touched** (reviewers should see zero diff here)
- `src/postgres_mcp/sql/sql_driver.py` (`DbConnPool` reused unchanged)
- `src/postgres_mcp/sql/safe_sql.py`, `sql/extension_utils.py`, `sql/bind_params.py`, `sql/index.py`
- `src/postgres_mcp/database_health/**` (constructor `__init__(self, sql_driver)` unchanged)
- `src/postgres_mcp/index/dta_calc.py`, `index/llm_opt.py`, `index/presentation.py` (internals)
- `src/postgres_mcp/explain/explain_plan.py`, `top_queries/top_queries_calc.py` (internals)
- `src/postgres_mcp/artifacts.py`



