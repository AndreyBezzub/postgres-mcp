# Changelog

All notable changes to this fork are documented here. Versions follow the fork
scheme in [`VERSIONING.md`](./VERSIONING.md): `vMAJOR.MINOR.PATCH-hc.N`, where
`MAJOR.MINOR.PATCH` is classified from Conventional Commits and `-hc.N` marks a
fork release. Release tags are cut on `main` after a squash-merge.

## v1.1.1-hc.1 — Remove pyright private-usage suppressions (refactor)

Removes the accumulated `# pyright: ignore[reportPrivateUsage]` "crutches" by giving tests a
legitimate public surface instead of loosening the type checker. `typeCheckingMode="standard"` +
`reportPrivateUsage=true` stay ON. Classified as a **PATCH** (internal `refactor:` — no public API
or runtime behavior change).

### Changed

- **Promoted four pure helpers from private to public** so tests reach them through a supported
  surface: `apply_env_allowlist` and `load_connections_file` (`server`), plus `build_db_url` and
  `discovery_dbname` (`DbConnPoolRegistry`).
- **Rewrote the white-box registry tests onto the public API.** Single-mode seeding now runs through
  `validate_and_register(base_url, None)` (with the `DbConnPool` factory patched) instead of poking
  `_mode` / `_pools`; the `build_db_url` / `discovery_dbname` tests call the public methods directly.

### Added

- **`DbConnPoolRegistry.is_open(name, environment=DEFAULT_ENV)`** — public introspection of whether a
  lazy pool has actually been opened, replacing the integration test's direct `_pools` access.

### Notes

- The **only** remaining `reportPrivateUsage` suppression is FastMCP-internal
  (`mcp._tool_manager.list_tools()` in `server.py`): the public `FastMCP.list_tools()` rebuilds fresh
  protocol copies per `tools/list` request, so an in-place parameter-description patch would not
  persist and must go through the internal manager. Migrating to standalone `fastmcp` 3.x
  (`ArgTransform`) is the eventual clean fix, tracked separately.
- No runtime behavior change.

## v1.1.0-hc.1 — Declarative multi-environment config (`--connections-file`)

Lets a **standalone** server enter multi-environment mode from a JSON file, with no Python launcher —
the multi-environment analogue of `--databases`. Classified as a **MINOR** bump (backward-compatible
`feat:`): existing single-host / `--databases` / `run_multi` callers are unaffected.

### Added

- **`--connections-file <path>` CLI flag.** Reads a JSON object mapping
  `environment -> {"base_dsn": str, "databases": [str, ...]}` and starts the server through the
  existing `run_multi` path. Mutually exclusive with `--databases`; on a missing file, invalid JSON,
  or a non-object / empty top level the server exits with a clear message. Per-environment validation
  stays non-fatal (unreachable / malformed entries are recorded in the availability map, never abort
  startup). The `LMHC_DB_ENVS` allowlist applies to the loaded map exactly as on the programmatic
  `run_multi` path.

## v1.0.0-hc.1 — Multi-environment (multi-server) support

**BREAKING CHANGE.** This release adds a multi-environment entry point and makes
`environment` a required argument on the SQL tools when the server runs in the
multi-environment mode. It is classified as a **MAJOR** bump (breaking behavior +
tool-surface change) even though the work landed on a `feat/` branch — the
squash-merge commit is marked `!` / `BREAKING CHANGE:` so the release tag becomes
`v1.0.0-hc.1`.

### Added

- **Multi-environment registry.** `DbConnPoolRegistry` now keys connection pools
  by `(environment, database)` instead of database-only. The single/`--databases`
  path keeps working via a synthetic default environment, so the existing
  `main()` behavior is unchanged.
- **New non-fatal startup path.** `register_environments()` probes every
  environment in parallel with short per-env timeouts and builds an availability
  map `env -> {reachable, dbs_ok, dbs_missing, error}`. It never raises and never
  calls `sys.exit`: the server ALWAYS starts, and a failing environment — an
  unreachable host (VPN/PG down), a probe timeout, OR a malformed/incomplete
  connection spec (missing or `None` `base_dsn`) — is simply marked unavailable
  with a reason. One bad environment entry never aborts the others.
- **`run_multi()` entry point** in `src/postgres_mcp/__init__.py`, mirroring
  `main()` (applies `WindowsSelectorEventLoopPolicy`, then
  `asyncio.run(server.run_multi(...))`). It receives an already-resolved
  `env -> {base_dsn, databases}` map. The fork stays credential-agnostic and
  replica-agnostic — a replica (e.g. `prod-replica`) is just another environment
  key with its own base DSN; the fork has no "replica" concept of its own.
- **`reconnect` tool** (side-effecting): re-probes environments that are
  currently unreachable and rebuilds their pools, returning the refreshed
  availability map — healthy environments and their in-flight queries are left
  untouched (no blast radius). An optional `environment` argument forces a
  re-probe of one specific environment regardless of its state. Enables recovery
  from an environment that went unreachable mid-session without restarting the
  MCP server (lazy per-touch recovery also works).
- **`LMHC_DB_ENVS` allowlist.** Unset → all provisioned environments active; set
  → filter to the listed names. Unknown names are dropped with a startup WARNING,
  never a crash.
- Tests: multi-environment unit tests (mock pool factory routed by host,
  non-fatal registration, `reconnect` recovery, lazy re-check, masking,
  environment-required guard) and integration tests using TWO local Docker
  Postgres containers via `tests/utils.create_postgres_container` (raw docker
  SDK, no `testcontainers`), one paused mid-test to prove non-fatal startup and
  `reconnect` recovery. No test connects to a real Aiven/HootCore host or needs
  VPN.

### Changed

- **All 9 SQL tools now take an `environment` argument** (`list_schemas`,
  `list_objects`, `get_object_details`, `explain_query`, `execute_sql`,
  `analyze_workload_indexes`, `analyze_query_indexes`, `analyze_db_health`,
  `get_top_queries`). On the multi-environment path both `environment` and
  `database_name` are required — there is no default DSN to fall back to.
- **`list_databases` enriched.** It stays parameter-less (global by design) and
  now returns the full per-environment availability map (available DBs +
  reachability + error) on the multi-environment path. The old
  `{"databases", "mode"}` shape is preserved on the single/`--databases` path.
- Password masking is unified on `obfuscate_password`
  (`sql/sql_driver.py`, exported via `sql/__init__.py`) for every string in the
  availability map and every error surface; no second masking implementation is
  introduced. Credentials are held in memory only — never in argv, on disk, or
  in a shared environment variable — and `run_multi` uses no `DATABASE_URI`.

### Downstream migration (HootCore — DOCUMENT ONLY, not edited by this release)

Consumers that address this server through the `lmhc-db` plugin move from the
per-environment servers (`pg-uat1`, `pg-uat2`, `pg-prep`, `pg-prod`,
`pg-prod-replica`) to a **single `pg` server**; the environment is now selected
by the `environment` tool argument (and gated by `LMHC_DB_ENVS`), not by the
server key. The MCP tool namespace therefore collapses to
`mcp__plugin_lmhc-db_pg__*`.

Two HootCore files must be updated **by the HootCore repo owner** (this release
does not touch them):

1. **`.claude/settings.json` → `permissions.allow`.** Replace the per-server
   `execute_sql` allow-entries:

   ```
   mcp__pg-prod__execute_sql
   mcp__pg-prod-replica__execute_sql
   mcp__plugin_pg-mcp_pg-prod__execute_sql
   mcp__plugin_pg-mcp_pg-prod-replica__execute_sql
   ```

   with the single new server key:

   ```
   mcp__plugin_lmhc-db_pg__execute_sql
   ```

2. **`.claude/settings.json` → `hooks.PreToolUse` (VPN ensure hook).** Change the
   matcher that currently fires the LMAUVPN tunnel check:

   ```
   "matcher": "mcp__(pg-|plugin_pg-mcp_pg-).*"
   ```

   to match the single server:

   ```
   "matcher": "mcp__plugin_lmhc-db_pg__.*"
   ```

Any saved allow-lists / memories that reference the old per-environment server
keys should be updated to the single-server namespace as well.

## v0.4.0-hc.1 — Multi-database support (single PG host)

Adds the ability for one server to expose several databases that live on the **same**
PostgreSQL host and share **one** set of credentials. Classified as a **MINOR** bump
(backward-compatible `feat:`): with no `--databases` flag the server behaves exactly as the
`0.3.0` upstream baseline.

### Added

- **`--databases` flag.** A comma-separated list of database names on the host named by
  `DATABASE_URI`. At startup the server connects once to the discovery database (the dbname in
  `DATABASE_URI`, or `postgres` if none), validates each requested name against `pg_database`,
  and registers only the valid ones. Names that do not exist or disallow connections are skipped
  with a warning; if none are valid the server exits.
- **Per-database connection pools (`DbConnPoolRegistry`).** One `DbConnPool` per validated
  database, all sharing the host/port/credentials from `DATABASE_URI` with only the dbname
  swapped. Pools open **lazily** — a database connects on the first tool call that targets it.
- **`list_databases` tool.** Returns the registered database names and the current mode
  (`single` or `multi`).

### Changed

- **Every SQL tool takes a `database_name` parameter**, selecting the target database per call.
  When `--databases` is omitted the server runs in single-DB mode and `database_name` defaults
  to the sole database, so existing single-database callers are unaffected.

### Known limitation

- `pg_stat_statements` is server-global: `get_top_queries` / `analyze_workload_indexes` report
  queries across the entire server regardless of `database_name`.
