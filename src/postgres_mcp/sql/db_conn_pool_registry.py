"""Registry of per-database connection pools sharing one set of credentials."""

import asyncio
import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Dict
from typing import List
from typing import Optional
from urllib.parse import urlparse
from urllib.parse import urlunparse

from .sql_driver import DbConnPool
from .sql_driver import obfuscate_password

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
        self._locks: Dict[str, asyncio.Lock] = {}
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

    async def validate_and_register(self, base_url: str, database_names: Optional[List[str]]) -> ValidationResult:
        """Validate requested DBs against pg_database and register pools (lazy, open=False)."""
        self._base_url = base_url
        self._discovery_db = self._discovery_dbname(base_url)

        if not database_names:  # single-DB mode
            self._mode = "single"
            name = self._discovery_db
            self._register(name)  # open=False, lazy
            return ValidationResult(registered=[name], missing=[], disallowed=[])

        self._mode = "multi"
        discovery = DbConnPool(self._build_db_url(self._discovery_db))
        try:
            try:
                pool = await discovery.pool_connect()  # opens discovery pool only
            except Exception as e:
                masked = obfuscate_password(self._build_db_url(self._discovery_db))
                raise DatabaseValidationError(f"Could not connect to discovery database '{self._discovery_db}' at {masked}: {e}") from e
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
            self._register(name)
        return ValidationResult(registered=registered, missing=missing, disallowed=[])

    def _register(self, name: str) -> None:
        """Store a lazy pool and its serialization lock for a database name."""
        self._pools[name] = DbConnPool(self._build_db_url(name))
        self._locks[name] = asyncio.Lock()

    async def get_pool(self, database_name: Optional[str]) -> DbConnPool:
        """Return the (lazily opened) pool for database_name, else raise DatabaseValidationError."""
        available = self.get_names()
        if not database_name:
            raise DatabaseValidationError(
                f"database_name is required. Available databases: {', '.join(available)}. Call list_databases for the current list.",
                available_databases=available,
            )
        pool = self._pools.get(database_name)
        if pool is None:
            raise DatabaseValidationError(
                f"Unknown database '{database_name}'. Available databases: {', '.join(available)}. Call list_databases for the current list.",
                available_databases=available,
            )
        # Serialize concurrent first-calls so two coroutines can't each open a pool.
        async with self._locks[database_name]:
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
        """Close every registered pool (used on shutdown)."""
        for pool in self._pools.values():
            await pool.close()

    def _build_db_url(self, dbname: str) -> str:
        parsed = urlparse(self._base_url or "")
        return urlunparse(parsed._replace(path=f"/{dbname}"))

    def _discovery_dbname(self, base_url: str) -> str:
        parsed = urlparse(base_url)
        return parsed.path.lstrip("/") or "postgres"
