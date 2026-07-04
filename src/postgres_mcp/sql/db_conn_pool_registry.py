"""Registry of connection pools keyed by (environment, database).

Two operating shapes share this registry:

* The **single / multi** path (driven by ``server.main()`` / the ``--databases``
  flag): there is exactly one PG server (one ``base_url``) and no real
  environment concept. Pools for that path are keyed under the synthetic
  :data:`DEFAULT_ENV` environment. Discovery failures on this path are FATAL
  (they propagate as :class:`DatabaseValidationError`, which ``main()`` turns
  into ``sys.exit``) — this preserves the historical behavior.

* The **multi-environment** path (driven by ``server.run_multi()``): many PG
  servers, one ``base_dsn`` per environment (``prod-replica`` is just another
  environment key — the fork stays replica-agnostic). Registration here is
  NON-FATAL: every environment is probed in parallel with a short timeout and
  the outcome is recorded in an availability map; an unreachable environment
  never aborts startup.
"""

import asyncio
import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Dict
from typing import List
from typing import Mapping
from typing import Optional
from typing import Tuple
from urllib.parse import urlparse
from urllib.parse import urlunparse

from .sql_driver import DbConnPool
from .sql_driver import obfuscate_password

logger = logging.getLogger(__name__)

# Environment key used by the single / multi main() path, which has no real
# environment concept (a single base_url). Real environments provisioned by the
# plugin (uat1/uat2/prep/prod/prod-replica) never use this name, so there is no
# collision with the multi-environment path.
DEFAULT_ENV = "default"

# Per-environment probe timeout on the non-fatal multi-environment path. Kept
# short so one unreachable environment (VPN/PG down) cannot stall startup.
PROBE_TIMEOUT_SECONDS = 5.0


@dataclass
class ValidationResult:
    registered: List[str]
    missing: List[str]
    disallowed: List[str] = field(default_factory=list)


@dataclass
class EnvAvailability:
    """Per-environment reachability snapshot used to build the availability map."""

    reachable: bool
    dbs_ok: List[str] = field(default_factory=list)
    dbs_missing: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reachable": self.reachable,
            "dbs_ok": list(self.dbs_ok),
            "dbs_missing": list(self.dbs_missing),
            "error": self.error,
        }


class DatabaseValidationError(Exception):
    """Raised when a tool targets an unknown / missing database_name or environment."""

    def __init__(self, message: str, available_databases: Optional[List[str]] = None):
        super().__init__(message)
        self.available_databases = available_databases or []


class DbConnPoolRegistry:
    """Holds one DbConnPool per (environment, database)."""

    def __init__(self) -> None:
        self._pools: Dict[Tuple[str, str], DbConnPool] = {}
        self._locks: Dict[Tuple[str, str], asyncio.Lock] = {}
        # Per-environment base connection URL / discovery dbname.
        self._base_urls: Dict[str, str] = {}
        self._discovery_dbs: Dict[str, str] = {}
        # Databases requested per environment (needed to re-probe on reconnect).
        self._requested_dbs: Dict[str, List[str]] = {}
        # Per-environment availability snapshot (multi-environment path only).
        self._availability: Dict[str, EnvAvailability] = {}
        self._mode: str = "single"  # "single" | "multi" (single/multi path only)
        self._multi_env: bool = False  # True once run_multi's register_environments ran
        # Backward-compatible mirrors for the single/multi path (also used by tests).
        self._base_url: Optional[str] = None
        self._discovery_db: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    @property
    def mode(self) -> str:
        """Return "single" or "multi" (single/multi main() path)."""
        return self._mode

    @property
    def multi_env(self) -> bool:
        """True when the registry was populated via the multi-environment path."""
        return self._multi_env

    def get_names(self) -> List[str]:
        """Database names registered under the default (single/multi) environment."""
        return self.get_names_for_env(DEFAULT_ENV)

    def get_names_for_env(self, environment: str) -> List[str]:
        """Database names registered under a given environment, in registration order."""
        return [db for (env, db) in self._pools if env == environment]

    def get_environments(self) -> List[str]:
        """All environments known on the multi-environment path, in registration order."""
        return list(self._availability.keys())

    def is_registered(self, name: str, environment: str = DEFAULT_ENV) -> bool:
        """True if (environment, name) is a registered pool."""
        return (environment, name) in self._pools

    def availability_map(self) -> Dict[str, Dict[str, Any]]:
        """Return the per-environment availability map (JSON-serializable)."""
        return {env: snap.to_dict() for env, snap in self._availability.items()}

    # ------------------------------------------------------------------ #
    # Single / multi path (server.main)
    # ------------------------------------------------------------------ #
    async def validate_and_register(self, base_url: str, database_names: Optional[List[str]]) -> ValidationResult:
        """Validate requested DBs against pg_database and register lazy pools (single/multi path).

        Discovery failures raise DatabaseValidationError (main() turns this into sys.exit).
        """
        self._base_url = base_url
        self._base_urls[DEFAULT_ENV] = base_url
        self._discovery_db = self._discovery_dbname(base_url)
        self._discovery_dbs[DEFAULT_ENV] = self._discovery_db

        if not database_names:  # single-DB mode
            self._mode = "single"
            name = self._discovery_db
            self._register(DEFAULT_ENV, name)  # open=False, lazy
            return ValidationResult(registered=[name], missing=[], disallowed=[])

        self._mode = "multi"
        registered, missing = await self._probe_env(DEFAULT_ENV, base_url, database_names)
        return ValidationResult(registered=registered, missing=missing, disallowed=[])

    # ------------------------------------------------------------------ #
    # Multi-environment path (server.run_multi)
    # ------------------------------------------------------------------ #
    async def register_environments(self, connections: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Non-fatally probe & register every environment, returning the availability map.

        ``connections`` maps ``environment -> {"base_dsn": str, "databases": [str, ...]}``.
        Each environment is probed in parallel with a short timeout; an unreachable
        environment is recorded as ``reachable=False`` with a masked reason and never
        aborts startup.

        This method NEVER raises: every per-environment failure — unreachable host, probe
        timeout, OR a malformed/incomplete spec (missing or ``None`` ``base_dsn``) — is
        captured in the returned availability map. This is the guarantee ``run_multi`` relies
        on to always start the server.
        """
        self._multi_env = True
        self._mode = "multi"

        async def probe_one(env: str, spec: Mapping[str, Any]) -> Tuple[str, EnvAvailability]:
            # NON-FATAL contract: a malformed or incomplete spec for ONE environment must be
            # recorded as unreachable, never raise — otherwise one bad entry would abort
            # startup for every environment. Every field access lives INSIDE the try, so a
            # missing "base_dsn" (KeyError) or a None base_dsn (urlparse TypeError) becomes a
            # recorded reason rather than an escaping exception.
            databases: List[str] = []
            try:
                databases = list(spec.get("databases") or [])
                base_dsn = spec.get("base_dsn")
                if not isinstance(base_dsn, str) or not base_dsn:
                    return env, EnvAvailability(
                        reachable=False,
                        dbs_ok=[],
                        dbs_missing=list(databases),
                        error=f"Invalid connection spec for environment '{env}': 'base_dsn' is missing or empty",
                    )
                self._base_urls[env] = base_dsn
                self._discovery_dbs[env] = self._discovery_dbname(base_dsn)
                self._requested_dbs[env] = databases
                registered, missing = await asyncio.wait_for(
                    self._probe_env(env, base_dsn, databases),
                    timeout=PROBE_TIMEOUT_SECONDS,
                )
                return env, EnvAvailability(reachable=True, dbs_ok=registered, dbs_missing=missing, error=None)
            except asyncio.TimeoutError:
                return env, EnvAvailability(
                    reachable=False,
                    dbs_ok=[],
                    dbs_missing=list(databases),
                    error=f"Timed out after {PROBE_TIMEOUT_SECONDS:g}s connecting to environment '{env}'",
                )
            except Exception as e:  # noqa: BLE001 - non-fatal by design
                return env, EnvAvailability(
                    reachable=False,
                    dbs_ok=[],
                    dbs_missing=list(databases),
                    error=obfuscate_password(str(e)),
                )

        # return_exceptions=True is defense-in-depth: probe_one is written never to raise,
        # but if a future change regresses that, fold the failure into the map instead of
        # letting it propagate out of run_multi and crash the whole server.
        items = list(connections.items())
        results = await asyncio.gather(
            *(probe_one(env, spec) for env, spec in items),
            return_exceptions=True,
        )
        # Assign in the caller's environment order (deterministic map ordering).
        for (env, _spec), res in zip(items, results):
            if isinstance(res, BaseException):
                self._availability[env] = EnvAvailability(
                    reachable=False,
                    dbs_ok=[],
                    dbs_missing=[],
                    error=obfuscate_password(str(res)) or "unexpected error during environment registration",
                )
            else:
                res_env, snap = res
                self._availability[res_env] = snap
        return self.availability_map()

    async def reconnect_all(self, environment: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Re-probe environments and rebuild their pools, returning the full availability map.

        By default only environments currently marked unreachable are re-probed, so healthy
        environments — and any in-flight queries against their pools — are left completely
        untouched (no blast radius on unrelated environments). Pass ``environment`` to force a
        re-probe of one specific environment regardless of its current state. Environments that
        are not re-probed keep their existing pools and availability entry. Non-fatal, like
        :meth:`register_environments`.
        """
        known = self.get_environments()
        if environment is not None:
            candidates = [environment] if environment in known else []
        else:
            candidates = [env for env, snap in self._availability.items() if not snap.reachable]
        # Skip environments that never registered a base DSN (a malformed spec): there is
        # nothing to reconnect to, and their availability entry is left as-is.
        targets = [env for env in candidates if env in self._base_urls]

        for env in targets:
            await self._close_env(env)  # closes ONLY this env's pools; healthy envs untouched
            self._availability.pop(env, None)

        if targets:
            connections = {env: {"base_dsn": self._base_urls[env], "databases": self._requested_dbs.get(env, [])} for env in targets}
            await self.register_environments(connections)
        return self.availability_map()

    async def _close_env(self, environment: str) -> None:
        """Close and drop every pool/lock registered under a single environment."""
        keys = [key for key in list(self._pools) if key[0] == environment]
        for key in keys:
            pool = self._pools.pop(key, None)
            self._locks.pop(key, None)
            if pool is not None:
                await pool.close()

    # ------------------------------------------------------------------ #
    # Pool access (both paths)
    # ------------------------------------------------------------------ #
    async def get_pool(self, database_name: Optional[str], environment: Optional[str] = None) -> DbConnPool:
        """Return the (lazily opened) pool for (environment, database_name).

        On the multi-environment path ``environment`` is REQUIRED. On the single/multi
        path ``environment`` defaults to :data:`DEFAULT_ENV`.
        """
        if self._multi_env:
            if not environment:
                raise DatabaseValidationError(
                    f"environment is required. Available environments: {', '.join(self.get_environments())}. "
                    f"Call list_databases for the current list.",
                )
            env = environment
        else:
            env = environment if environment is not None else DEFAULT_ENV

        available = self.get_names_for_env(env)
        if not database_name:
            raise DatabaseValidationError(
                f"database_name is required. Available databases: {', '.join(available)}. Call list_databases for the current list.",
                available_databases=available,
            )
        key = (env, database_name)
        pool = self._pools.get(key)
        if pool is None:
            raise DatabaseValidationError(
                f"Unknown database '{database_name}'. Available databases: {', '.join(available)}. Call list_databases for the current list.",
                available_databases=available,
            )
        # Serialize concurrent first-calls so two coroutines can't each open a pool.
        async with self._locks[key]:
            try:
                await pool.pool_connect()  # idempotent: returns existing pool if valid, else opens
            except DatabaseValidationError:
                raise
            except Exception as e:
                raise DatabaseValidationError(
                    f"Could not open connection to database '{database_name}': {obfuscate_password(str(e))}",
                    available_databases=available,
                ) from e
        return pool

    async def close_all(self) -> None:
        """Close every registered pool (used on shutdown / before a reconnect rebuild)."""
        for pool in self._pools.values():
            await pool.close()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _probe_env(self, environment: str, base_url: str, database_names: List[str]) -> Tuple[List[str], List[str]]:
        """Open a discovery pool for ``environment``, validate ``database_names`` against
        pg_database, and register the valid ones lazily. Returns ``(registered, missing)``.

        Raises DatabaseValidationError if the discovery database is unreachable. Callers on
        the non-fatal path wrap this and record the failure in the availability map.
        """
        discovery_db = self._discovery_dbname(base_url)
        discovery_url = self._build_db_url(discovery_db, base_url)
        discovery = DbConnPool(discovery_url)
        try:
            try:
                pool = await discovery.pool_connect()  # opens discovery pool only
            except Exception as e:
                masked_url = obfuscate_password(discovery_url)
                raise DatabaseValidationError(
                    f"Could not connect to discovery database '{discovery_db}' at {masked_url}: {obfuscate_password(str(e))}"
                ) from e
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT datname FROM pg_database WHERE datname = ANY(%s) AND datallowconn = true",
                        [database_names],
                    )
                    valid = {row[0] for row in await cur.fetchall()}
        finally:
            await discovery.close()  # discovery pool not retained

        registered = [n for n in database_names if n in valid]
        missing = [n for n in database_names if n not in valid]
        for name in registered:  # lazy: store, do not open
            self._register(environment, name, base_url)
        return registered, missing

    def _register(self, environment: str, name: str, base_url: Optional[str] = None) -> None:
        """Store a lazy pool and its serialization lock for (environment, name)."""
        url = self._build_db_url(name, base_url if base_url is not None else self._base_urls.get(environment))
        key = (environment, name)
        self._pools[key] = DbConnPool(url)
        self._locks[key] = asyncio.Lock()

    def _build_db_url(self, dbname: str, base_url: Optional[str] = None) -> str:
        """Swap the path (dbname) on a base URL, preserving credentials/host/query."""
        parsed = urlparse((base_url if base_url is not None else self._base_url) or "")
        return urlunparse(parsed._replace(path=f"/{dbname}"))

    def _discovery_dbname(self, base_url: str) -> str:
        parsed = urlparse(base_url)
        return parsed.path.lstrip("/") or "postgres"
