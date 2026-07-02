# Changelog

All notable changes to this fork are documented here. Versions follow the fork
scheme in [`VERSIONING.md`](./VERSIONING.md): `vMAJOR.MINOR.PATCH-hc.N`, where
`MAJOR.MINOR.PATCH` is classified from Conventional Commits and `-hc.N` marks a
fork release. Release tags are cut on `main` after a squash-merge.

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
  calls `sys.exit`: the server ALWAYS starts, and an unreachable environment
  (VPN/PG down) is simply marked unavailable with a reason.
- **`run_multi()` entry point** in `src/postgres_mcp/__init__.py`, mirroring
  `main()` (applies `WindowsSelectorEventLoopPolicy`, then
  `asyncio.run(server.run_multi(...))`). It receives an already-resolved
  `env -> {base_dsn, databases}` map. The fork stays credential-agnostic and
  replica-agnostic — a replica (e.g. `prod-replica`) is just another environment
  key with its own base DSN; the fork has no "replica" concept of its own.
- **`reconnect` tool** (side-effecting): re-probes every active environment,
  rebuilds pools, and returns the refreshed availability map. Enables recovery
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
