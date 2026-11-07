"""The SQL Server extractor (spec section 3): builds a catalog snapshot through a read-only
login, first cut.

It checks its own permissions before reading anything and fails loudly, because a missing
grant makes SQL Server return empty results silently. It reads only metadata views and
server properties: never a row of a client table, never client code executed.

First cut: server and database settings, every user table's columns, and every view's
definition with its stored settings. The rest of the catalog (constraints, routines,
triggers, msdb jobs, references, the raw capture, secrets) is later work.
"""
from __future__ import annotations

import dataclasses

from ...core.catalog import CatalogObject, Fact, Provenance, Snapshot
from .connection import Connection

SOURCE = "sqlserver"
MSDB_TABLES = ("sysjobs", "sysjobsteps", "sysschedules", "sysjobschedules", "syscategories")
# A plain login holds these on the server; anything more is more than read-only needs.
SERVER_PERMISSIONS_ALLOWED = {"CONNECT SQL", "VIEW ANY DATABASE"}
# msdb roles that can create or run jobs (spec section 3: no fixed role, SQLAgentReaderRole included).
MSDB_ROLES = ("SQLAgentUserRole", "SQLAgentReaderRole", "SQLAgentOperatorRole", "db_owner", "db_datawriter",
              "db_ddladmin")
# Effective permissions on client objects, whether granted to the login, a role or public.
EFFECTIVE_OBJECT_PERMISSIONS = """
SELECT QUOTENAME(s.name) + '.' + QUOTENAME(o.name) AS object_name, p.permission_name
FROM sys.objects AS o
JOIN sys.schemas AS s ON s.schema_id = o.schema_id
CROSS APPLY (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'), ('EXECUTE')) AS p (permission_name)
WHERE o.is_ms_shipped = 0 AND o.type IN ('U', 'V', 'P', 'PC', 'FN', 'IF', 'TF', 'FS', 'FT')
  AND HAS_PERMS_BY_NAME(QUOTENAME(s.name) + '.' + QUOTENAME(o.name), 'OBJECT', p.permission_name) = 1
ORDER BY object_name, p.permission_name"""
LISTED = 20


@dataclasses.dataclass(frozen=True)
class ServerAddress:
    host: str
    port: int
    login: str
    password: str


class ExtractorPermissionError(Exception):
    """The extractor's login lacks a permission it needs, or holds one it must not."""

    def __init__(self, login: str, missing: list[str], excess: list[str]):
        self.login, self.missing, self.excess = login, missing, excess
        lines = [f"The extractor's login {login!r} failed its permission self-check; nothing was read."]
        lines += [f"  missing: {m}" for m in missing]
        lines += [f"  must not hold: {e}" for e in excess]
        lines.append("Grant exactly the set in Annex F of the v1 architecture spec, then extract again.")
        super().__init__("\n".join(lines))


def _db_permissions_allowed(permission: str) -> bool:
    # CONNECT and the VIEW ... DEFINITION family that comes with VIEW DEFINITION (Annex F, check 1).
    return permission == "CONNECT" or (permission.startswith("VIEW ") and permission.endswith(" DEFINITION"))


def _capped(items: list[str]) -> list[str]:
    return items if len(items) <= LISTED else items[:LISTED] + [f"... and {len(items) - LISTED} more"]


def check_permissions(address: ServerAddress, databases: list[str]) -> None:
    """The self-check (spec section 3, promise 6). Raises ExtractorPermissionError listing
    everything missing and everything held beyond the read-only grant set."""
    missing: list[str] = []
    excess: list[str] = []
    with Connection(address.host, address.port, address.login, address.password) as master:
        server = {r["permission_name"] for r in master.query(
            "SELECT permission_name FROM fn_my_permissions(NULL, 'SERVER')")}
        excess += [f"server permission {p}" for p in sorted(server - SERVER_PERMISSIONS_ALLOWED)]
        access = {r["name"]: r["has_access"] for r in master.query(
            "SELECT name, HAS_DBACCESS(name) AS has_access FROM sys.databases")}
        for database in [*databases, "msdb"]:
            if database not in access:
                missing.append(f"database {database}: not on the server, or not visible to the login")
            elif access[database] != 1:
                missing.append(f"database {database}: a user for the login")
    if access.get("msdb") == 1:
        with Connection(address.host, address.port, address.login, address.password, "msdb") as msdb:
            for table in MSDB_TABLES:
                held = msdb.query("SELECT HAS_PERMS_BY_NAME(@name, 'OBJECT', 'SELECT') AS held",
                                  {"name": f"dbo.{table}"})[0]["held"]
                if held != 1:
                    missing.append(f"database msdb: SELECT on dbo.{table}")
            for role in MSDB_ROLES:
                if msdb.query("SELECT IS_ROLEMEMBER(@role) AS member", {"role": role})[0]["member"] == 1:
                    excess.append(f"database msdb: member of {role}")
            held = {r["permission_name"] for r in msdb.query(
                "SELECT permission_name FROM fn_my_permissions(NULL, 'DATABASE')")}
            excess += [f"database msdb: {p}" for p in sorted(held) if not _db_permissions_allowed(p)]
    for database in databases:
        if access.get(database) != 1:
            continue
        with Connection(address.host, address.port, address.login, address.password, database) as conn:
            held = {r["permission_name"] for r in conn.query(
                "SELECT permission_name FROM fn_my_permissions(NULL, 'DATABASE')")}
            if "VIEW DEFINITION" not in held:
                missing.append(f"database {database}: VIEW DEFINITION")
            excess += [f"database {database}: {p}" for p in sorted(held) if not _db_permissions_allowed(p)]
            # Reading rows, writing or executing any client object, however it was granted.
            excess += _capped([f"database {database}: {r['permission_name']} on {r['object_name']}"
                               for r in conn.query(EFFECTIVE_OBJECT_PERMISSIONS)])
    if missing or excess:
        raise ExtractorPermissionError(address.login, missing, excess)


def _server(value_view: str) -> Provenance:
    return Provenance("server", value_view)


def extract(address: ServerAddress, databases: list[str]) -> Snapshot:
    """Check permissions, then read the catalog of the named databases into a snapshot."""
    check_permissions(address, databases)
    objects: list[CatalogObject] = []
    with Connection(address.host, address.port, address.login, address.password) as master:
        objects.append(_server_object(master))
    for database in databases:
        with Connection(address.host, address.port, address.login, address.password, database) as conn:
            objects += _database_objects(conn, database)
    return Snapshot.of(SOURCE, objects)


def _server_object(conn: Connection) -> CatalogObject:
    row = conn.query(
        "SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)) AS version,"
        " CAST(SERVERPROPERTY('Edition') AS nvarchar(128)) AS edition,"
        " CAST(SERVERPROPERTY('Collation') AS nvarchar(128)) AS collation,"
        " CAST(SERVERPROPERTY('ServerName') AS nvarchar(128)) AS server_name")[0]
    props = _server("SERVERPROPERTY")
    facts = [Fact("skeleton", "version", row["version"], props),
             Fact("skeleton", "edition", row["edition"], props),
             Fact("skeleton", "collation", row["collation"], props)]
    # Fails on SQL Server 2017 and earlier, where the time zone becomes a required setting
    # (later work); until then the failure stops the extraction rather than guessing.
    zone = conn.query("SELECT CURRENT_TIMEZONE() AS zone")[0]["zone"]
    facts.append(Fact("skeleton", "time_zone", zone, _server("CURRENT_TIMEZONE()")))
    return CatalogObject(row["server_name"], "server", tuple(facts))


def _database_objects(conn: Connection, database: str) -> list[CatalogObject]:
    db = conn.query(
        "SELECT name, collation_name, compatibility_level, is_ansi_nulls_on, is_quoted_identifier_on,"
        " is_recursive_triggers_on FROM sys.databases WHERE database_id = DB_ID()")[0]
    sysdb = _server("sys.databases")
    objects = [CatalogObject(database, "database", (
        Fact("skeleton", "collation", db["collation_name"], sysdb),
        Fact(SOURCE, "compatibility_level", int(db["compatibility_level"]), sysdb),
        Fact(SOURCE, "ansi_nulls_default", bool(db["is_ansi_nulls_on"]), sysdb),
        Fact(SOURCE, "quoted_identifier_default", bool(db["is_quoted_identifier_on"]), sysdb),
        Fact(SOURCE, "recursive_triggers", bool(db["is_recursive_triggers_on"]), sysdb),
    ))]

    columns: dict[tuple[str, str], list[dict]] = {}
    for r in conn.query(
            "SELECT s.name AS schema_name, t.name AS table_name, c.column_id, c.name AS column_name,"
            " TYPE_NAME(c.system_type_id) AS type_name, TYPE_NAME(c.user_type_id) AS declared_type,"
            " c.max_length, c.precision, c.scale, c.is_nullable, c.collation_name, c.is_identity,"
            " c.is_computed"
            " FROM sys.tables AS t JOIN sys.schemas AS s ON s.schema_id = t.schema_id"
            " JOIN sys.columns AS c ON c.object_id = t.object_id"
            " WHERE t.is_ms_shipped = 0 ORDER BY s.name, t.name, c.column_id"):
        columns.setdefault((r["schema_name"], r["table_name"]), []).append({
            "name": r["column_name"],
            "declared_type": r["declared_type"],
            "system_type": r["type_name"],
            "max_length": int(r["max_length"]),
            "precision": int(r["precision"]),
            "scale": int(r["scale"]),
            "nullable": bool(r["is_nullable"]),
            "collation": r["collation_name"],
            "identity": bool(r["is_identity"]),
            "computed": bool(r["is_computed"]),
        })
    syscols = _server("sys.tables, sys.columns")
    for (schema, table), cols in sorted(columns.items()):
        objects.append(CatalogObject(f"{database}.{schema}.{table}", "table", (
            Fact("skeleton", "columns", cols, syscols),
        )))

    sysmod = _server("sys.views, sys.sql_modules")
    for r in conn.query(
            "SELECT s.name AS schema_name, v.name AS view_name, m.definition, m.uses_ansi_nulls,"
            " m.uses_quoted_identifier, OBJECTPROPERTY(v.object_id, 'IsEncrypted') AS is_encrypted"
            " FROM sys.views AS v JOIN sys.schemas AS s ON s.schema_id = v.schema_id"
            " LEFT JOIN sys.sql_modules AS m ON m.object_id = v.object_id"
            " WHERE v.is_ms_shipped = 0 ORDER BY s.name, v.name"):
        facts = [Fact("skeleton", "source_text", r["definition"], sysmod),
                 Fact("skeleton", "extractable", r["definition"] is not None and r["is_encrypted"] != 1, sysmod)]
        if r["definition"] is not None:
            facts += [Fact(SOURCE, "ansi_nulls", bool(r["uses_ansi_nulls"]), sysmod),
                      Fact(SOURCE, "quoted_identifier", bool(r["uses_quoted_identifier"]), sysmod)]
        objects.append(CatalogObject(f"{database}.{r['schema_name']}.{r['view_name']}", "view", tuple(facts)))
    return objects
