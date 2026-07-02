import asyncio
import sys
from typing import Any
from typing import Dict

from . import server
from . import top_queries


def _apply_windows_event_loop_policy() -> None:
    # As of version 3.3.0 Psycopg on Windows is not compatible with the default
    # ProactorEventLoop.
    # See: https://www.psycopg.org/psycopg3/docs/advanced/async.html#async
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def main():
    """Main entry point for the package (single / multi-database on one PG server)."""
    _apply_windows_event_loop_policy()
    asyncio.run(server.main())


def run_multi(
    connections: Dict[str, Any],
    access_mode: str = "restricted",
    transport: str = "stdio",
) -> None:
    """Multi-environment entry point (credential- and replica-agnostic).

    Mirrors ``main()``: applies the Windows event-loop policy fix, then runs the
    ``server.run_multi`` coroutine. ``connections`` maps
    ``environment -> {"base_dsn": str, "databases": [str, ...]}`` (already resolved
    by the caller). Startup is non-fatal — unreachable environments are recorded in
    the availability map rather than aborting the server.
    """
    _apply_windows_event_loop_policy()
    asyncio.run(server.run_multi(connections=connections, access_mode=access_mode, transport=transport))


# Optionally expose other important items at package level
__all__ = [
    "main",
    "run_multi",
    "server",
    "top_queries",
]
