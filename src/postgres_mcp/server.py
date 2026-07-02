# ruff: noqa: B008
import argparse
import asyncio
import logging
import os
import signal
import sys
from enum import Enum
from typing import Any
from typing import List
from typing import Literal
from typing import Optional
from typing import Union

import mcp.types as types
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field
from pydantic import validate_call

from postgres_mcp.index.dta_calc import DatabaseTuningAdvisor

from .artifacts import ErrorResult
from .artifacts import ExplainPlanArtifact
from .database_health import DatabaseHealthTool
from .database_health import HealthType
from .explain import ExplainPlanTool
from .index.index_opt_base import MAX_NUM_INDEX_TUNING_QUERIES
from .index.llm_opt import LLMOptimizerTool
from .index.presentation import TextPresentation
from .sql import DatabaseValidationError
from .sql import DbConnPoolRegistry
from .sql import SafeSqlDriver
from .sql import SqlDriver
from .sql import check_hypopg_installation_status
from .sql import obfuscate_password
from .top_queries import TopQueriesCalc

# Initialize FastMCP with default settings
mcp = FastMCP("postgres-mcp")

# Constants
PG_STAT_STATEMENTS = "pg_stat_statements"
HYPOPG_EXTENSION = "hypopg"
DATABASE_NAME_PARAM_DESC = "Target database name. Call list_databases for available names."
ENVIRONMENT_PARAM_DESC = "Target environment. Required in multi-environment mode; call list_databases for available environments."
PG_STAT_STATEMENTS_SCOPE_NOTE = (
    f"\n\nNote: {PG_STAT_STATEMENTS} has server-global scope; results include queries from all "
    "databases on this PG server, not just the selected database_name."
)

ResponseType = List[types.TextContent | types.ImageContent | types.EmbeddedResource]

logger = logging.getLogger(__name__)


class AccessMode(str, Enum):
    """SQL access modes for the server."""

    UNRESTRICTED = "unrestricted"  # Unrestricted access
    RESTRICTED = "restricted"  # Read-only with safety features


# Global variables
db_registry = DbConnPoolRegistry()
current_access_mode = AccessMode.UNRESTRICTED
shutdown_in_progress = False


async def get_sql_driver(database_name: Optional[str], environment: Optional[str] = None) -> Union[SqlDriver, SafeSqlDriver]:
    """Get the SQL driver for a specific (environment, database), honoring the current access mode.

    On the multi-environment path both ``environment`` and ``database_name`` are required
    (enforced by the registry). On the single/multi path ``environment`` is unused and a
    sole single-mode database is resolved by default for backward compatibility.
    """
    if not db_registry.multi_env and db_registry.mode == "single" and database_name is None:
        database_name = db_registry.get_names()[0]  # backward-compatible default
    pool = await db_registry.get_pool(database_name, environment)  # raises DatabaseValidationError if None/unknown
    base_driver = SqlDriver(conn=pool)
    if current_access_mode == AccessMode.RESTRICTED:
        return SafeSqlDriver(sql_driver=base_driver, timeout=30)
    return base_driver


def format_text_response(text: Any) -> ResponseType:
    """Format a text response."""
    return [types.TextContent(type="text", text=str(text))]


def format_error_response(error: str) -> ResponseType:
    """Format an error response."""
    return format_text_response(f"Error: {error}")


@mcp.tool(
    description="List all schemas in the database",
    annotations=ToolAnnotations(
        title="List Schemas",
        readOnlyHint=True,
    ),
)
async def list_schemas(
    environment: Optional[str] = Field(None, description=ENVIRONMENT_PARAM_DESC),
    database_name: Optional[str] = Field(None, description=DATABASE_NAME_PARAM_DESC),
) -> ResponseType:
    """List all schemas in the database."""
    try:
        sql_driver = await get_sql_driver(database_name, environment)
        rows = await sql_driver.execute_query(
            """
            SELECT
                schema_name,
                schema_owner,
                CASE
                    WHEN schema_name LIKE 'pg_%' THEN 'System Schema'
                    WHEN schema_name = 'information_schema' THEN 'System Information Schema'
                    ELSE 'User Schema'
                END as schema_type
            FROM information_schema.schemata
            ORDER BY schema_type, schema_name
            """
        )
        schemas = [row.cells for row in rows] if rows else []
        return format_text_response(schemas)
    except Exception as e:
        logger.error(f"Error listing schemas: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="List objects in a schema",
    annotations=ToolAnnotations(
        title="List Objects",
        readOnlyHint=True,
    ),
)
async def list_objects(
    environment: Optional[str] = Field(None, description=ENVIRONMENT_PARAM_DESC),
    database_name: Optional[str] = Field(None, description=DATABASE_NAME_PARAM_DESC),
    schema_name: str = Field(description="Schema name"),
    object_type: str = Field(description="Object type: 'table', 'view', 'sequence', or 'extension'", default="table"),
) -> ResponseType:
    """List objects of a given type in a schema."""
    try:
        sql_driver = await get_sql_driver(database_name, environment)

        if object_type in ("table", "view"):
            table_type = "BASE TABLE" if object_type == "table" else "VIEW"
            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT table_schema, table_name, table_type
                FROM information_schema.tables
                WHERE table_schema = {} AND table_type = {}
                ORDER BY table_name
                """,
                [schema_name, table_type],
            )
            objects = (
                [{"schema": row.cells["table_schema"], "name": row.cells["table_name"], "type": row.cells["table_type"]} for row in rows]
                if rows
                else []
            )

        elif object_type == "sequence":
            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT sequence_schema, sequence_name, data_type
                FROM information_schema.sequences
                WHERE sequence_schema = {}
                ORDER BY sequence_name
                """,
                [schema_name],
            )
            objects = (
                [{"schema": row.cells["sequence_schema"], "name": row.cells["sequence_name"], "data_type": row.cells["data_type"]} for row in rows]
                if rows
                else []
            )

        elif object_type == "extension":
            # Extensions are not schema-specific
            rows = await sql_driver.execute_query(
                """
                SELECT extname, extversion, extrelocatable
                FROM pg_extension
                ORDER BY extname
                """
            )
            objects = (
                [{"name": row.cells["extname"], "version": row.cells["extversion"], "relocatable": row.cells["extrelocatable"]} for row in rows]
                if rows
                else []
            )

        else:
            return format_error_response(f"Unsupported object type: {object_type}")

        return format_text_response(objects)
    except Exception as e:
        logger.error(f"Error listing objects: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Show detailed information about a database object",
    annotations=ToolAnnotations(
        title="Get Object Details",
        readOnlyHint=True,
    ),
)
async def get_object_details(
    environment: Optional[str] = Field(None, description=ENVIRONMENT_PARAM_DESC),
    database_name: Optional[str] = Field(None, description=DATABASE_NAME_PARAM_DESC),
    schema_name: str = Field(description="Schema name"),
    object_name: str = Field(description="Object name"),
    object_type: str = Field(description="Object type: 'table', 'view', 'sequence', or 'extension'", default="table"),
) -> ResponseType:
    """Get detailed information about a database object."""
    try:
        sql_driver = await get_sql_driver(database_name, environment)

        if object_type in ("table", "view"):
            # Get columns
            col_rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = {} AND table_name = {}
                ORDER BY ordinal_position
                """,
                [schema_name, object_name],
            )
            columns = (
                [
                    {
                        "column": r.cells["column_name"],
                        "data_type": r.cells["data_type"],
                        "is_nullable": r.cells["is_nullable"],
                        "default": r.cells["column_default"],
                    }
                    for r in col_rows
                ]
                if col_rows
                else []
            )

            # Get constraints
            con_rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT tc.constraint_name, tc.constraint_type, kcu.column_name
                FROM information_schema.table_constraints AS tc
                LEFT JOIN information_schema.key_column_usage AS kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                WHERE tc.table_schema = {} AND tc.table_name = {}
                """,
                [schema_name, object_name],
            )

            constraints = {}
            if con_rows:
                for row in con_rows:
                    cname = row.cells["constraint_name"]
                    ctype = row.cells["constraint_type"]
                    col = row.cells["column_name"]

                    if cname not in constraints:
                        constraints[cname] = {"type": ctype, "columns": []}
                    if col:
                        constraints[cname]["columns"].append(col)

            constraints_list = [{"name": name, **data} for name, data in constraints.items()]

            # Get indexes
            idx_rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = {} AND tablename = {}
                """,
                [schema_name, object_name],
            )

            indexes = [{"name": r.cells["indexname"], "definition": r.cells["indexdef"]} for r in idx_rows] if idx_rows else []

            result = {
                "basic": {"schema": schema_name, "name": object_name, "type": object_type},
                "columns": columns,
                "constraints": constraints_list,
                "indexes": indexes,
            }

        elif object_type == "sequence":
            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT sequence_schema, sequence_name, data_type, start_value, increment
                FROM information_schema.sequences
                WHERE sequence_schema = {} AND sequence_name = {}
                """,
                [schema_name, object_name],
            )

            if rows and rows[0]:
                row = rows[0]
                result = {
                    "schema": row.cells["sequence_schema"],
                    "name": row.cells["sequence_name"],
                    "data_type": row.cells["data_type"],
                    "start_value": row.cells["start_value"],
                    "increment": row.cells["increment"],
                }
            else:
                result = {}

        elif object_type == "extension":
            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT extname, extversion, extrelocatable
                FROM pg_extension
                WHERE extname = {}
                """,
                [object_name],
            )

            if rows and rows[0]:
                row = rows[0]
                result = {"name": row.cells["extname"], "version": row.cells["extversion"], "relocatable": row.cells["extrelocatable"]}
            else:
                result = {}

        else:
            return format_error_response(f"Unsupported object type: {object_type}")

        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error getting object details: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Explains the execution plan for a SQL query, showing how the database will execute it and provides detailed cost estimates.",
    annotations=ToolAnnotations(
        title="Explain Query",
        readOnlyHint=True,
    ),
)
async def explain_query(
    environment: Optional[str] = Field(None, description=ENVIRONMENT_PARAM_DESC),
    database_name: Optional[str] = Field(None, description=DATABASE_NAME_PARAM_DESC),
    sql: str = Field(description="SQL query to explain"),
    analyze: bool = Field(
        description="When True, actually runs the query to show real execution statistics instead of estimates. "
        "Takes longer but provides more accurate information.",
        default=False,
    ),
    hypothetical_indexes: list[dict[str, Any]] = Field(
        description="""A list of hypothetical indexes to simulate. Each index must be a dictionary with these keys:
    - 'table': The table name to add the index to (e.g., 'users')
    - 'columns': List of column names to include in the index (e.g., ['email'] or ['last_name', 'first_name'])
    - 'using': Optional index method (default: 'btree', other options include 'hash', 'gist', etc.)

Examples: [
    {"table": "users", "columns": ["email"], "using": "btree"},
    {"table": "orders", "columns": ["user_id", "created_at"]}
]
If there is no hypothetical index, you can pass an empty list.""",
        default=[],
    ),
) -> ResponseType:
    """
    Explains the execution plan for a SQL query.

    Args:
        sql: The SQL query to explain
        analyze: When True, actually runs the query for real statistics
        hypothetical_indexes: Optional list of indexes to simulate
    """
    try:
        sql_driver = await get_sql_driver(database_name, environment)
        explain_tool = ExplainPlanTool(sql_driver=sql_driver)
        result: ExplainPlanArtifact | ErrorResult | None = None

        # If hypothetical indexes are specified, check for HypoPG extension
        if hypothetical_indexes and len(hypothetical_indexes) > 0:
            if analyze:
                return format_error_response("Cannot use analyze and hypothetical indexes together")
            try:
                # Use the common utility function to check if hypopg is installed
                (
                    is_hypopg_installed,
                    hypopg_message,
                ) = await check_hypopg_installation_status(sql_driver)

                # If hypopg is not installed, return the message
                if not is_hypopg_installed:
                    return format_text_response(hypopg_message)

                # HypoPG is installed, proceed with explaining with hypothetical indexes
                result = await explain_tool.explain_with_hypothetical_indexes(sql, hypothetical_indexes)
            except Exception:
                raise  # Re-raise the original exception
        elif analyze:
            try:
                # Use EXPLAIN ANALYZE
                result = await explain_tool.explain_analyze(sql)
            except Exception:
                raise  # Re-raise the original exception
        else:
            try:
                # Use basic EXPLAIN
                result = await explain_tool.explain(sql)
            except Exception:
                raise  # Re-raise the original exception

        if result and isinstance(result, ExplainPlanArtifact):
            return format_text_response(result.to_text())
        else:
            error_message = "Error processing explain plan"
            if isinstance(result, ErrorResult):
                error_message = result.to_text()
            return format_error_response(error_message)
    except Exception as e:
        logger.error(f"Error explaining query: {e}")
        return format_error_response(str(e))


# Query function declaration without the decorator - we'll add it dynamically based on access mode
async def execute_sql(
    environment: Optional[str] = Field(None, description=ENVIRONMENT_PARAM_DESC),
    database_name: Optional[str] = Field(None, description=DATABASE_NAME_PARAM_DESC),
    sql: str = Field(description="SQL to run", default="all"),
) -> ResponseType:
    """Executes a SQL query against the database."""
    try:
        sql_driver = await get_sql_driver(database_name, environment)
        rows = await sql_driver.execute_query(sql)  # type: ignore
        if rows is None:
            return format_text_response("No results")
        return format_text_response(list([r.cells for r in rows]))
    except Exception as e:
        logger.error(f"Error executing query: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Analyze frequently executed queries in the database and recommend optimal indexes",
    annotations=ToolAnnotations(
        title="Analyze Workload Indexes",
        readOnlyHint=True,
    ),
)
@validate_call
# environment and database_name placed last (both optional-with-default) due to
# @validate_call's required-before-optional constraint; requiredness on the
# multi-environment path is enforced at runtime by the registry.
async def analyze_workload_indexes(
    max_index_size_mb: int = Field(description="Max index size in MB", default=10000),
    method: Literal["dta", "llm"] = Field(description="Method to use for analysis", default="dta"),
    environment: Optional[str] = Field(None, description=ENVIRONMENT_PARAM_DESC),
    database_name: Optional[str] = Field(None, description=DATABASE_NAME_PARAM_DESC),
) -> ResponseType:
    """Analyze frequently executed queries in the database and recommend optimal indexes."""
    try:
        sql_driver = await get_sql_driver(database_name, environment)
        if method == "dta":
            index_tuning = DatabaseTuningAdvisor(sql_driver)
        else:
            index_tuning = LLMOptimizerTool(sql_driver)
        dta_tool = TextPresentation(sql_driver, index_tuning)
        result = await dta_tool.analyze_workload(max_index_size_mb=max_index_size_mb)
        return format_text_response(f"{result}{PG_STAT_STATEMENTS_SCOPE_NOTE}")
    except Exception as e:
        logger.error(f"Error analyzing workload: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Analyze a list of (up to 10) SQL queries and recommend optimal indexes",
    annotations=ToolAnnotations(
        title="Analyze Query Indexes",
        readOnlyHint=True,
    ),
)
@validate_call
# environment and database_name placed last (both optional-with-default) due to
# @validate_call's required-before-optional constraint; requiredness on the
# multi-environment path is enforced at runtime by the registry.
async def analyze_query_indexes(
    queries: list[str] = Field(description="List of Query strings to analyze"),
    max_index_size_mb: int = Field(description="Max index size in MB", default=10000),
    method: Literal["dta", "llm"] = Field(description="Method to use for analysis", default="dta"),
    environment: Optional[str] = Field(None, description=ENVIRONMENT_PARAM_DESC),
    database_name: Optional[str] = Field(None, description=DATABASE_NAME_PARAM_DESC),
) -> ResponseType:
    """Analyze a list of SQL queries and recommend optimal indexes."""
    if len(queries) == 0:
        return format_error_response("Please provide a non-empty list of queries to analyze.")
    if len(queries) > MAX_NUM_INDEX_TUNING_QUERIES:
        return format_error_response(f"Please provide a list of up to {MAX_NUM_INDEX_TUNING_QUERIES} queries to analyze.")

    try:
        sql_driver = await get_sql_driver(database_name, environment)
        if method == "dta":
            index_tuning = DatabaseTuningAdvisor(sql_driver)
        else:
            index_tuning = LLMOptimizerTool(sql_driver)
        dta_tool = TextPresentation(sql_driver, index_tuning)
        result = await dta_tool.analyze_queries(queries=queries, max_index_size_mb=max_index_size_mb)
        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error analyzing queries: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Analyzes database health. Here are the available health checks:\n"
    "- index - checks for invalid, duplicate, and bloated indexes\n"
    "- connection - checks the number of connection and their utilization\n"
    "- vacuum - checks vacuum health for transaction id wraparound\n"
    "- sequence - checks sequences at risk of exceeding their maximum value\n"
    "- replication - checks replication health including lag and slots\n"
    "- buffer - checks for buffer cache hit rates for indexes and tables\n"
    "- constraint - checks for invalid constraints\n"
    "- all - runs all checks\n"
    "You can optionally specify a single health check or a comma-separated list of health checks. The default is 'all' checks.",
    annotations=ToolAnnotations(
        title="Analyze Database Health",
        readOnlyHint=True,
    ),
)
async def analyze_db_health(
    environment: Optional[str] = Field(None, description=ENVIRONMENT_PARAM_DESC),
    database_name: Optional[str] = Field(None, description=DATABASE_NAME_PARAM_DESC),
    health_type: str = Field(
        description=f"Optional. Valid values are: {', '.join(sorted([t.value for t in HealthType]))}.",
        default="all",
    ),
) -> ResponseType:
    """Analyze database health for specified components.

    Args:
        health_type: Comma-separated list of health check types to perform.
                    Valid values: index, connection, vacuum, sequence, replication, buffer, constraint, all
    """
    try:
        sql_driver = await get_sql_driver(database_name, environment)
        health_tool = DatabaseHealthTool(sql_driver)
        result = await health_tool.health(health_type=health_type)
        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error analyzing database health: {e}")
        return format_error_response(str(e))


@mcp.tool(
    name="get_top_queries",
    description=f"Reports the slowest or most resource-intensive queries using data from the '{PG_STAT_STATEMENTS}' extension.",
    annotations=ToolAnnotations(
        title="Get Top Queries",
        readOnlyHint=True,
    ),
)
async def get_top_queries(
    environment: Optional[str] = Field(None, description=ENVIRONMENT_PARAM_DESC),
    database_name: Optional[str] = Field(None, description=DATABASE_NAME_PARAM_DESC),
    sort_by: str = Field(
        description="Ranking criteria: 'total_time' for total execution time or 'mean_time' for mean execution time per call, or 'resources' "
        "for resource-intensive queries",
        default="resources",
    ),
    limit: int = Field(description="Number of queries to return when ranking based on mean_time or total_time", default=10),
) -> ResponseType:
    try:
        sql_driver = await get_sql_driver(database_name, environment)
        top_queries_tool = TopQueriesCalc(sql_driver=sql_driver)

        if sort_by == "resources":
            result = await top_queries_tool.get_top_resource_queries()
            return format_text_response(f"{result}{PG_STAT_STATEMENTS_SCOPE_NOTE}")
        elif sort_by == "mean_time" or sort_by == "total_time":
            # Map the sort_by values to what get_top_queries_by_time expects
            result = await top_queries_tool.get_top_queries_by_time(limit=limit, sort_by="mean" if sort_by == "mean_time" else "total")
        else:
            return format_error_response("Invalid sort criteria. Please use 'resources' or 'mean_time' or 'total_time'.")
        return format_text_response(f"{result}{PG_STAT_STATEMENTS_SCOPE_NOTE}")
    except Exception as e:
        logger.error(f"Error getting slow queries: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="List the databases this server is configured to access.",
    annotations=ToolAnnotations(
        title="List Databases",
        readOnlyHint=True,
    ),
)
async def list_databases() -> dict[str, Any]:
    """Return the availability view of the databases this server can access.

    On the multi-environment path this is a global (no-argument) status surface:
    ``{"mode": "multi-env", "environments": {env: {reachable, dbs_ok, dbs_missing,
    error}}}``. On the single/multi path the historical shape is preserved:
    ``{"databases": [...], "mode": "single"|"multi"}``. No side effects.
    """
    if db_registry.multi_env:
        return {"mode": "multi-env", "environments": db_registry.availability_map()}
    return {"databases": db_registry.get_names(), "mode": db_registry.mode}


@mcp.tool(
    description="Re-probe all active environments and rebuild the per-environment availability map. "
    "Use this to recover after an environment was unreachable (e.g. VPN/PG came back) without restarting the server.",
    annotations=ToolAnnotations(
        title="Reconnect",
        readOnlyHint=False,
    ),
)
async def reconnect() -> dict[str, Any]:
    """Side-effecting: re-probe every active environment and return the refreshed map."""
    if not db_registry.multi_env:
        return {
            "mode": db_registry.mode,
            "error": "reconnect is only available in multi-environment mode.",
        }
    try:
        availability = await db_registry.reconnect_all()
        return {"mode": "multi-env", "environments": availability}
    except Exception as e:
        logger.error(f"Error during reconnect: {obfuscate_password(str(e))}")
        return {"mode": "multi-env", "error": obfuscate_password(str(e))}


def _inject_param_description(param_name: str, desc: str) -> None:
    """Patch a parameter's description on every registered tool that exposes it.

    Mutates tool.parameters in place; FastMCP reads this dict by reference when
    serving protocol-level list_tools, so the change is visible immediately.
    Verified on mcp >=1.25.0.
    """
    # Guards against future mcp SDK changes to the internal tool-registry API:
    # a broken description patch must not abort server startup.
    try:
        for tool in mcp._tool_manager.list_tools():  # pyright: ignore[reportPrivateUsage]
            props = tool.parameters.get("properties", {})
            if param_name in props:
                props[param_name]["description"] = desc
    except Exception as e:
        logger.warning(f"Could not inject {param_name} descriptions: {e}")


def _inject_database_name_description(desc: str) -> None:
    """Backward-compatible wrapper: patch the database_name description on every tool."""
    _inject_param_description("database_name", desc)


def _register_execute_sql_tool() -> None:
    """Register execute_sql with a description/annotations appropriate to the access mode.

    execute_sql has no @mcp.tool decorator; it is added dynamically here so its
    read-only vs destructive presentation follows current_access_mode. Shared by
    both main() (single/multi path) and run_multi() (multi-environment path).
    """
    if current_access_mode == AccessMode.UNRESTRICTED:
        mcp.add_tool(
            execute_sql,
            description="Execute any SQL query",
            annotations=ToolAnnotations(
                title="Execute SQL",
                destructiveHint=True,
            ),
        )
    else:
        mcp.add_tool(
            execute_sql,
            description="Execute a read-only SQL query",
            annotations=ToolAnnotations(
                title="Execute SQL (Read-Only)",
                readOnlyHint=True,
            ),
        )


def _apply_env_allowlist(connections: dict[str, Any]) -> dict[str, Any]:
    """Filter ``connections`` by the ``LMHC_DB_ENVS`` allowlist (multi-environment path).

    Unset/blank -> all provisioned environments active. Set -> keep only listed names;
    unknown names are dropped with a WARNING (never a crash). Filtering to an empty set
    is honored (the server still starts, with zero active environments).
    """
    raw = os.environ.get("LMHC_DB_ENVS")
    if not raw or not raw.strip():
        return dict(connections)
    requested = list(dict.fromkeys(e.strip() for e in raw.split(",") if e.strip()))
    provisioned = set(connections)
    for name in requested:
        if name not in provisioned:
            logger.warning("LMHC_DB_ENVS: ignoring unknown environment '%s' (not provisioned)", name)
    filtered = {name: connections[name] for name in requested if name in provisioned}
    if not filtered:
        logger.warning("LMHC_DB_ENVS filtered out every environment; the server will start with zero active environments")
    return filtered


async def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="PostgreSQL MCP Server")
    parser.add_argument("database_url", help="Database connection URL", nargs="?")
    parser.add_argument(
        "--access-mode",
        type=str,
        choices=[mode.value for mode in AccessMode],
        default=AccessMode.UNRESTRICTED.value,
        help="Set SQL access mode: unrestricted (unrestricted) or restricted (read-only with protections)",
    )
    parser.add_argument(
        "--databases",
        type=str,
        default=None,
        help="Comma-separated database names on the same PG server to expose (multi-DB mode). "
        "If omitted, single-DB mode uses the dbname from DATABASE_URI.",
    )
    parser.add_argument(
        "--transport",
        type=str,
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Select MCP transport: stdio (default), sse, or streamable-http",
    )
    parser.add_argument(
        "--sse-host",
        type=str,
        default="localhost",
        help="Host to bind SSE server to (default: localhost)",
    )
    parser.add_argument(
        "--sse-port",
        type=int,
        default=8000,
        help="Port for SSE server (default: 8000)",
    )
    parser.add_argument(
        "--streamable-http-host",
        type=str,
        default="localhost",
        help="Host to bind streamable HTTP server to (default: localhost)",
    )
    parser.add_argument(
        "--streamable-http-port",
        type=int,
        default=8000,
        help="Port for streamable HTTP server (default: 8000)",
    )

    args = parser.parse_args()

    # Store the access mode in the global variable
    global current_access_mode
    current_access_mode = AccessMode(args.access_mode)

    database_names = list(dict.fromkeys(d.strip() for d in args.databases.split(",") if d.strip())) if args.databases else None

    # Add the query tool with a description and annotations appropriate to the access mode
    _register_execute_sql_tool()

    logger.info(f"Starting PostgreSQL MCP Server in {current_access_mode.upper()} mode")

    # Get database URL from environment variable or command line
    database_url = os.environ.get("DATABASE_URI", args.database_url)

    if not database_url:
        raise ValueError(
            "Error: No database URL provided. Please specify via 'DATABASE_URI' environment variable or command-line argument.",
        )

    # Initialize the database registry (lazy pools)
    global db_registry
    try:
        result = await db_registry.validate_and_register(database_url, database_names)
    except DatabaseValidationError as e:
        # Multi-DB mode: discovery DB unreachable. Single-DB mode never connects here
        # (lazy first-connection), so this only fires when --databases was provided.
        logger.error(f"Discovery database connection failed: {e}")
        sys.exit(1)

    if database_names and len(result.registered) == 0:
        logger.error("None of the requested databases are available: %s", ", ".join(result.missing))
        sys.exit(1)
    if result.missing:
        logger.warning("Skipping databases not found / not connectable: %s", ", ".join(result.missing))
    logger.info(
        "Registered %d database(s) in %s mode: %s",
        len(result.registered),
        db_registry.mode,
        ", ".join(result.registered),
    )

    # dynamic database_name description injection (Phase 2)
    names = db_registry.get_names()
    desc = f"Target database. Available: {', '.join(names)}. Required in multi-DB mode; call list_databases for the current list."
    globals()["DATABASE_NAME_PARAM_DESC"] = desc
    _inject_database_name_description(desc)

    # Set up proper shutdown handling
    try:
        loop = asyncio.get_running_loop()
        signals = (signal.SIGTERM, signal.SIGINT)
        for s in signals:
            loop.add_signal_handler(s, lambda s=s: asyncio.create_task(shutdown(s)))
    except NotImplementedError:
        # Windows doesn't support signals properly
        logger.warning("Signal handling not supported on Windows")
        pass

    # Run the server with the selected transport (always async)
    if args.transport == "stdio":
        await mcp.run_stdio_async()
    elif args.transport == "sse":
        mcp.settings.host = args.sse_host
        mcp.settings.port = args.sse_port
        await mcp.run_sse_async()
    elif args.transport == "streamable-http":
        mcp.settings.host = args.streamable_http_host
        mcp.settings.port = args.streamable_http_port
        await mcp.run_streamable_http_async()


async def run_multi(
    connections: dict[str, Any],
    access_mode: str = AccessMode.RESTRICTED.value,
    transport: str = "stdio",
    sse_host: str = "localhost",
    sse_port: int = 8000,
    streamable_http_host: str = "localhost",
    streamable_http_port: int = 8000,
) -> None:
    """Multi-environment entry point (credential- and replica-agnostic).

    ``connections`` maps ``environment -> {"base_dsn": str, "databases": [str, ...]}``,
    already resolved by the caller (the plugin). ``prod-replica`` is simply another
    environment key. This path is NON-FATAL: every environment is probed in parallel
    with a short timeout and unreachable ones are recorded in the availability map —
    the server ALWAYS starts and NEVER calls sys.exit on a per-environment failure.
    """
    global current_access_mode
    current_access_mode = AccessMode(access_mode)

    # Register execute_sql (access-mode dependent) exactly as the single/multi path does.
    _register_execute_sql_tool()

    logger.info(f"Starting PostgreSQL MCP Server (multi-environment) in {current_access_mode.upper()} mode")

    # Apply the LMHC_DB_ENVS allowlist over the provisioned environments.
    active = _apply_env_allowlist(connections)

    # Non-fatal, parallel per-environment probing -> availability map.
    global db_registry
    availability = await db_registry.register_environments(active)

    for env, info in availability.items():
        if info["reachable"]:
            msg = f"Environment '{env}' reachable: {len(info['dbs_ok'])} database(s) available"
            if info["dbs_missing"]:
                msg += f"; {len(info['dbs_missing'])} not found: {', '.join(info['dbs_missing'])}"
            logger.info(msg)
        else:
            logger.warning("Environment '%s' UNAVAILABLE (server still starting): %s", env, info["error"])
    logger.info(
        "Active environments: %s",
        ", ".join(db_registry.get_environments()) or "(none)",
    )

    # Dynamic parameter-description injection for the multi-environment tool surface.
    envs = db_registry.get_environments()
    env_desc = (
        f"Target environment. Available: {', '.join(envs) or '(none)'}. "
        "Required; call list_databases for the current list."
    )
    globals()["ENVIRONMENT_PARAM_DESC"] = env_desc
    _inject_param_description("environment", env_desc)
    db_desc = "Target database within the selected environment. Required; call list_databases for available names."
    globals()["DATABASE_NAME_PARAM_DESC"] = db_desc
    _inject_param_description("database_name", db_desc)

    # Set up proper shutdown handling (mirrors main()).
    try:
        loop = asyncio.get_running_loop()
        signals = (signal.SIGTERM, signal.SIGINT)
        for s in signals:
            loop.add_signal_handler(s, lambda s=s: asyncio.create_task(shutdown(s)))
    except NotImplementedError:
        # Windows doesn't support signals properly
        logger.warning("Signal handling not supported on Windows")

    # Run the server with the selected transport (always async).
    if transport == "stdio":
        await mcp.run_stdio_async()
    elif transport == "sse":
        mcp.settings.host = sse_host
        mcp.settings.port = sse_port
        await mcp.run_sse_async()
    elif transport == "streamable-http":
        mcp.settings.host = streamable_http_host
        mcp.settings.port = streamable_http_port
        await mcp.run_streamable_http_async()


async def shutdown(sig=None):
    """Clean shutdown of the server."""
    global shutdown_in_progress

    if shutdown_in_progress:
        logger.warning("Forcing immediate exit")
        # Use sys.exit instead of os._exit to allow for proper cleanup
        sys.exit(1)

    shutdown_in_progress = True

    if sig:
        logger.info(f"Received exit signal {sig.name}")

    # Close database connections
    try:
        await db_registry.close_all()
        logger.info("Closed database connections")
    except Exception as e:
        logger.error(f"Error closing database connections: {e}")

    # Exit with appropriate status code
    sys.exit(128 + sig if sig is not None else 0)
