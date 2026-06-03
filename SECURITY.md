# Security

This document explains how to deploy this PostgreSQL MCP server **safely**. It is written
for anyone running the server against a real database — local, staging, or production.

> **One-line summary:** the MCP client is an autonomous LLM that writes its own SQL.
> Treat it as an untrusted SQL generator and enforce read-only **at the database layer**,
> not only at the application layer. A correctly locked-down PostgreSQL role is the
> authoritative control; the server's `restricted` access mode is a useful second line of
> defense, not a substitute for it.

---

## 1. Threat model

The thing issuing SQL is a large language model. Two distinct concerns follow:

1. **Integrity** — the model must not mutate data (INSERT/UPDATE/DELETE/DDL) or cause
   side effects (advance sequences, call writing functions, reach external systems).
2. **Availability** — the model must not run unbounded queries that saturate CPU/IO,
   hold long MVCC snapshots (blocking autovacuum), or exhaust connections. On a busy
   production database a single accidental cross join is a self-inflicted outage.

Confidentiality (which rows/columns the model may *read*) is **out of scope** for this
server — neither access mode masks or row-restricts data. If some data must never be read
by the model, do not grant SELECT on it (see §5).

---

## 2. The two supported secure postures

There is no single "secure setting". Two postures are both valid; pick one and configure
it fully:

| | Posture A — defense in depth | Posture B — DB-enforced |
|---|---|---|
| Access mode | `restricted` | `unrestricted` |
| Primary guard | pglast allow-list **and** DB role | DB role only |
| Analytics | limited (see §6) | full |
| `EXPLAIN ANALYZE` | blocked | allowed |
| Hard requirement | a read-only DB role | a **hardened** read-only DB role (§5) |

**Both postures require a read-only database role.** The access mode only changes whether
the application adds a second, independent layer on top. Running `unrestricted` against a
role that can write is the one configuration that is genuinely unsafe.

---

## 3. How the access modes work

The mode is chosen with `--access-mode` (`server.py`); the default is `unrestricted`.

### `unrestricted`
`get_sql_driver` returns the bare `SqlDriver`. `execute_sql` runs the query with
`force_readonly=False`, so there is **no statement validation, no timeout, and no
read-only transaction** — the query executes and is `COMMIT`ted (`sql_driver.py`,
`_execute_with_connection`). All safety therefore rests on the database role.

### `restricted`
`get_sql_driver` wraps the driver in `SafeSqlDriver` with a fixed 30-second timeout. Three
independent protections apply (`sql/safe_sql.py`):

1. **AST allow-list.** The query is parsed with `pglast` and every node is checked against
   `ALLOWED_STMT_TYPES`, `ALLOWED_NODE_TYPES`, `ALLOWED_FUNCTIONS`, and
   `ALLOWED_EXTENSIONS`. Anything not on the list is rejected. Only SELECT / EXPLAIN /
   SHOW / VACUUM / ANALYZE and a curated set of functions pass.
2. **Read-only transaction.** Execution is forced through `BEGIN TRANSACTION READ ONLY`
   and `ROLLBACK`ed afterwards, so even an allowed statement cannot write.
3. **Statement timeout.** A hard 30 s limit (not configurable via CLI).

It also explicitly rejects `EXPLAIN ANALYZE` (`safe_sql.py`, the `ExplainStmt` check) and
locking clauses (`SELECT … FOR UPDATE`). Note that `CREATE EXTENSION` — like all DDL — is
blocked wholesale by the read-only transaction (protection #2), regardless of
`ALLOWED_EXTENSIONS`; that allow-list gates only the parser and is **not** a list of
extensions considered safe to install (it includes egress-capable ones such as `dblink`,
`postgres_fdw`, `pg_net`).

---

## 4. Core principle: two independent layers

Keep these two ideas separate — they block different things and a secure deployment relies
on the **database** one:

- **Application layer** = the access mode. Env-independent, but only as complete as the
  allow-list, and (in `unrestricted`) absent entirely.
- **Database layer** = the PostgreSQL role's grants **and** its session settings. The
  **grants** are the authoritative control — they cannot be bypassed by the shape of the
  SQL. The **session settings** (e.g. `default_transaction_read_only`) are a strong default
  on top, but are USERSET-overridable (see §5.2), so they are a second layer, not the
  guarantee.

### "Read-only role" ≠ "read-only transaction"
A common trap. They are not the same and do not cover the same attacks:

- A **read-only role** (grant-based: `GRANT SELECT`, no INSERT/UPDATE/DELETE) blocks
  *direct DML on tables*. It does **not** stop side effects reachable from a `SELECT`:
  `SECURITY DEFINER` functions running as a privileged owner, `nextval()` advancing a
  sequence, or `dblink`/`postgres_fdw`/`pg_net` reaching other systems.
- A **read-only transaction** (`default_transaction_read_only = on`, behavior-based)
  blocks writes that happen *within that transaction* — `nextval()`, and write side effects
  of a `SECURITY DEFINER` function executed inline (regardless of who owns it). It does
  **not** reach side effects that leave the transaction: `dblink`/`postgres_fdw`/`pg_net`
  run over a **separate connection** whose transaction state is independent of the caller's,
  and OS-level functions (`COPY … TO PROGRAM`, `lo_export`) act outside the SQL transaction
  entirely. Those are closed by §5.1 (do not install the extensions; `NOSUPERUSER`), not by
  this setting.

`unrestricted` mode removes the read-only transaction that `restricted` provides. To stay
safe you must restore it at the role level (§5). Grants alone are necessary but not
sufficient.

---

## 5. Database role hardening (the authoritative control)

Create a dedicated, minimal login role for the MCP server. Do not reuse an application or
admin role.

### 5.1 Least-privilege role

```sql
-- Dedicated, non-inheriting, no special attributes.
CREATE ROLE mcp_readonly LOGIN PASSWORD '…'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

-- Read-only grants, per database the server will expose:
GRANT CONNECT ON DATABASE my_db TO mcp_readonly;
GRANT USAGE ON SCHEMA public TO mcp_readonly;          -- repeat per schema
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_readonly;

-- Cover future tables too (otherwise new tables are unreadable AND the posture drifts):
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO mcp_readonly;
```

Then **withhold** everything that lets a `SELECT` cause a write or escape:

- No `INSERT/UPDATE/DELETE/TRUNCATE` on any table.
- No `USAGE`/`UPDATE` on sequences → blocks `nextval()`.
- No `CREATE` on the database or any schema → cannot create its own writable tables.
- No `EXECUTE` on `SECURITY DEFINER` functions; do not install `dblink`, `postgres_fdw`,
  `pg_net`, `file_fdw`, `aws_s3`, or `lo` for this role's reach.
- Keep `NOSUPERUSER` and grant no membership in `pg_execute_server_program`,
  `pg_read_server_files`, or `pg_write_server_files` → blocks OS-level egress
  (`COPY … TO PROGRAM`, `lo_export`, `pg_read_file`). The read-only session setting does
  **not** cover these — they act outside the SQL transaction.
- Keep `NOINHERIT` or ensure the role is not a member of any privileged group.

### 5.2 Session settings — restore the two guards lost in `unrestricted`

Set these **on the role** so every login gets them automatically:

```sql
ALTER ROLE mcp_readonly SET statement_timeout = '30s';
ALTER ROLE mcp_readonly SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE mcp_readonly SET default_transaction_read_only = on;
```

| Setting | Closes |
|---|---|
| `statement_timeout` | Availability: caps each query's **server-side** execution at 30 s (the role-level equivalent of the app's per-call timeout in `restricted`). |
| `idle_in_transaction_session_timeout` | A session holding a transaction open indefinitely. |
| `default_transaction_read_only = on` | Integrity: blocks writes (incl. `nextval` and most `SECURITY DEFINER` side effects) for any transaction that stays read-only. The **second** layer, not the first — overridable, see the caveat below. |

> **Caveat — this setting is overridable, by design.** `default_transaction_read_only` is
> a `USERSET` parameter (`pg_settings.context = 'user'`): any session can issue
> `SET default_transaction_read_only = off` and the protection is gone — it is **not**
> immune to SQL shape. What still blocks writes after such an override is the §5.1 grant
> layer (no DML, no sequence privilege), nothing else.

With these three settings **and** the §5.1 grants, `unrestricted` is as safe as (or safer
than) `restricted` for a fully hardened role. But the layers are not interchangeable: the
**grants** (§5.1) are the guarantee that cannot be evaded by query shape; the session
settings are a strong default on top, not a substitute. If the grant layer drifts — a new
table with INSERT, a sequence `USAGE` grant, a `SECURITY DEFINER` function — the read-only
default does **not** back you up, because a session can switch it off.

---

## 6. Choosing an access mode

Both postures from §2 are secure **once §5 is done**. The difference is functionality:

- **`restricted`** adds a parser-level safety net that is independent of the database
  configuration. The cost is reduced capability:
  - Functions outside `ALLOWED_FUNCTIONS` are rejected — notably `generate_series`,
    statistical aggregates (`stddev`, `variance`, `var_pop`, `corr`, `regr_*`), and
    `date_bin`. Common analytical queries fail with `Function X is not allowed`.
  - `EXPLAIN ANALYZE` (real wall-clock / actual rows) is blocked; only cost-based
    `EXPLAIN` works.
  - The 30 s timeout is fixed and not configurable.
- **`unrestricted`** removes those limits — full analytical SQL and `EXPLAIN ANALYZE` work
  — and relies entirely on the §5 role. Choose this when the role is hardened and you need
  unrestricted read analytics.

Rule of thumb: if you cannot guarantee the role hardening in §5 across every environment,
stay on `restricted`. If you can, `unrestricted` + a hardened role is both safer to reason
about (no allow-list gaps) and more capable.

---

## 7. Connection and secret handling

- Pass the DSN via the **`DATABASE_URI` environment variable**, not as a command-line
  argument (argv is visible in process listings). The server reads `DATABASE_URI` first
  and falls back to the positional arg.
- **Always require TLS** to the database: include `?sslmode=require` (or stricter,
  `verify-full`) in the DSN. Never send credentials over an unencrypted connection.
- Store the password in a secret manager / OS credential store, not in shell history,
  repo files, or MCP client config committed to git.
- Use a **dedicated** role per consumer so credentials can be rotated and audited
  independently.

---

## 8. Transport security

`--transport` selects `stdio` (default), `sse`, or `streamable-http`.

- **`stdio`** is the safest default: the server is a child process of the MCP client with
  no network listener. Prefer it unless you specifically need a network transport.
- **`sse` / `streamable-http`** open a TCP listener. They bind to `localhost` by default
  (`--sse-host` / `--streamable-http-host`). **Do not bind to `0.0.0.0`** or expose the
  port publicly — the server has no authentication of its own, so anyone who can reach the
  port can run queries as the database role. If remote access is required, place it behind
  an authenticating reverse proxy / tunnel and keep the bind on `localhost`.

---

## 9. Operational caveats

- **Role settings apply at login.** `ALTER ROLE … SET …` only affects *new* connections.
  A running server (and any connection pool) keeps its existing sessions with the old
  values. After changing role config you must **restart / reconnect the server** before
  the change is effective. Verify with `current_setting(...)` in a live session — do not
  trust `pg_roles.rolconfig` alone (that only proves it is *configured*, not *active*).
- **Multi-DB blast radius.** With `--databases a,b,c` the same login role is used for every
  exposed database. The read-only guarantee must hold on **all** of them (and on any
  database added later).
- **Replicas.** A physical/logical replica that shares the same role inherits the same
  config automatically. A read replica is a good target for heavy analytical reads, but a
  long read still holds a snapshot — `statement_timeout` matters there too.

---

## 10. Verification checklist

Run these as the MCP role (substitute the real database) to prove the posture. The first
five should all show **0**; the last should show your configured values.

```sql
-- Identity & attributes (expect: not superuser, no createdb/createrole/bypassrls)
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls
FROM pg_roles WHERE rolname = current_user;

-- Writable tables (expect 0 insertable / 0 updatable)
SELECT count(*) FILTER (WHERE has_table_privilege(current_user, c.oid, 'INSERT')) AS insertable,
       count(*) FILTER (WHERE has_table_privilege(current_user, c.oid, 'UPDATE')) AS updatable
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema');

-- Sequence privileges → nextval (expect 0)
SELECT count(*) FILTER (WHERE has_sequence_privilege(current_user, c.oid, 'USAGE')) AS usage_seq,
       count(*) FILTER (WHERE has_sequence_privilege(current_user, c.oid, 'UPDATE')) AS update_seq
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'S' AND n.nspname NOT IN ('pg_catalog','information_schema');

-- SECURITY DEFINER functions the role may execute (expect 0)
SELECT count(*) FILTER (WHERE has_function_privilege(current_user, p.oid, 'EXECUTE')) AS secdef_executable
FROM pg_proc p WHERE p.prosecdef;

-- Object creation (expect db_create=false, creatable_schemas=0)
SELECT has_database_privilege(current_user, current_database(), 'CREATE') AS db_create,
       (SELECT count(*) FILTER (WHERE has_schema_privilege(current_user, n.oid, 'CREATE'))
        FROM pg_namespace n WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')) AS creatable_schemas;

-- Dangerous extensions present (expect empty)
SELECT extname FROM pg_extension
WHERE extname IN ('dblink','postgres_fdw','pg_net','file_fdw','aws_s3','lo','plpython3u','plperlu');

-- EFFECTIVE session settings (expect 30s / 1min / on) — run in a fresh connection
SELECT current_setting('statement_timeout'),
       current_setting('idle_in_transaction_session_timeout'),
       current_setting('default_transaction_read_only');
```

---

## Appendix A — HootCore reference deployment

A concrete example of Posture B, as deployed for the HootCore platform (verified
2026-06-03). Use it as a template; the values are environment-specific.

- **Distribution:** the fork ships to the team via the internal `hc-ai-tooling`
  marketplace and is wired into the `db`/`infra` MCP profiles; servers are named
  `pg-{env}` for `env ∈ {prep, prod, prod-replica, uat2}`, each in multi-DB mode
  (`lm-platform-data`, `hc-platform-oms-data`, `lm-e-commerce`, …).
- **Role:** every server connects as **`mcp_readonly`**, a managed Aiven role.
- **Credentials & secrets:** sourced from Vault at provisioning time, cached locally
  (host/port/user in `connections.json`, password in the Windows Credential Manager) so
  MCP start-up is Vault-free.

Verified role state (prep & prod identical):

| Check | Result |
|---|---|
| superuser / createdb / createrole / bypassrls | all `false` |
| group membership | only `pg_read_all_stats` (read-only) |
| INSERT/UPDATE on tables (all DBs) | **0** |
| sequence USAGE/UPDATE | **0** (nextval blocked) |
| `SECURITY DEFINER` functions | **0** |
| `dblink`/`postgres_fdw`/`pg_net`/… | none installed |
| CREATE on db / schemas | `false` / **0** |

Hardening applied on the Aiven role and verified effective in live sessions on
prep / prod / prod-replica:

```sql
ALTER ROLE mcp_readonly SET statement_timeout = '30s';
ALTER ROLE mcp_readonly SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE mcp_readonly SET default_transaction_read_only = on;
```

```text
statement_timeout                     = 30s
idle_in_transaction_session_timeout   = 1min
default_transaction_read_only         = on
```

Because the role meets every §5 requirement, HootCore can run these servers in
`--access-mode unrestricted` to unlock full analytical SQL and `EXPLAIN ANALYZE` without
weakening the read-only guarantee. The `ALTER ROLE` change required an Aiven admin (the
role cannot alter itself) and a server reconnect to take effect (see §9).
