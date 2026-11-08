"""The SQL Server extractor (spec section 3): builds a catalog snapshot through a read-only
login, for SQL Server 2017 and later.

It checks the server's version and its own permissions before reading anything, and fails
loudly, because a missing grant makes SQL Server return empty results silently. It reads
metadata views only, and each one only as `SELECT * FROM <view>`: every column of every
view it reads is kept wholesale as the raw capture, and every catalog fact is built from
those rows in Python, so the raw capture holds exactly what the facts came from and the
extractor reads nothing else (`statements` lists all it sends). Besides the views it calls
SERVERPROPERTY and CURRENT_TIMEZONE(), and the self-check's permission functions. It never
reads a row of a client table, never runs client code and never holds EXECUTE.

The catalog levels: the server; each database; tables with their columns, constraints and
row counts from sys.partitions; views, procedures and functions with their source text,
parameters, result shapes where metadata gives them, and stored settings; triggers; other
objects (synonyms, user-defined types and table types, sequences, and any other kind,
recorded by its type); SQL Agent jobs from msdb; references worked out by the parser, with
SQL Server's dependency metadata as a cross-check; and likely secrets, marked.

Not here, and reported where they matter: procedure result shapes, which need EXECUTE and
are described in the sandbox (issue #41); servers before 2017 (issue #74).

Folders of files are read beside the server or without one (folders.py): query batches,
SSIS packages and SQL Agent job scripts, and, for a database read from no server, the data
definition that builds its catalog. Every job's references are worked out from its steps
(jobs.py), whichever input it came from.
"""
from __future__ import annotations

import copy
import dataclasses
import decimal
import json
from typing import Any, Sequence

from ...core import secrets as core_secrets
from ...core.catalog import IGNORED_FOR_IDENTITY as IGNORED_FACT, CatalogObject, Fact, Provenance, Snapshot
from . import folders, jobs, names, parser, references, resolution, secrets
from .names import split_name
from .connection import Connection, SqlServerError

SOURCE = "sqlserver"
MSDB_TABLES = ("sysjobs", "sysjobsteps", "sysschedules", "sysjobschedules", "syscategories")
# Every metadata view the extractor reads, by where it reads it. Nothing else is read.
SERVER_VIEWS = ("sys.configurations", "sys.servers")
DATABASE_VIEWS = ("sys.databases", "sys.schemas", "sys.objects", "sys.tables", "sys.views", "sys.columns", "sys.types",
                  "sys.default_constraints", "sys.check_constraints", "sys.key_constraints", "sys.foreign_keys",
                  "sys.foreign_key_columns", "sys.indexes", "sys.index_columns", "sys.identity_columns",
                  "sys.computed_columns", "sys.sql_modules", "sys.parameters", "sys.triggers", "sys.trigger_events",
                  "sys.synonyms", "sys.sequences", "sys.table_types", "sys.partitions")
DEPENDENCY_VIEW = "sys.sql_expression_dependencies"   # read only where SELECT on it is granted (optional)
MSDB_VIEWS = tuple(f"dbo.{t}" for t in MSDB_TABLES)
# What the snapshot's identity leaves out (ADR 0023, amended), and nothing but the report may
# read. Raw-capture columns that change with nobody acting on the client's objects: log
# truncation state, SQL Agent's next run and run history, partition row counts (which
# Query Store's internal tables move by themselves, and from which a table's row count is
# built), and the counters that move with the client's rows: an identity's last value and a
# sequence's current and last used values. And volatile informational facts: a table's row
# count. These lists change only by a recorded decision; a test holds them to the ADR.
IGNORED_FOR_IDENTITY = {
    "sys.databases": ("log_reuse_wait", "log_reuse_wait_desc"),
    "sys.identity_columns": ("last_value",),
    "sys.sequences": ("current_value", "last_used_value"),
    "dbo.sysjobschedules": ("next_run_date", "next_run_time"),
    "dbo.sysjobsteps": ("last_run_outcome", "last_run_duration", "last_run_retries", "last_run_date",
                        "last_run_time"),
    "sys.partitions": ("rows",),
}
INFORMATIONAL_FACTS = {"table": ("row_count",)}
# Rows of these views that describe something outside the database being read are left out.
ROW_FILTERS = {"sys.databases": " WHERE database_id = DB_ID()"}
SERVER_PROPERTIES = ("ProductVersion", "ProductMajorVersion", "ProductLevel", "ProductUpdateLevel", "ProductBuild",
                     "Edition", "EngineEdition", "Collation", "ServerName")
SUPPORTED_MAJOR_VERSIONS = {14: "2017", 15: "2019", 16: "2022", 17: "2025"}
# SERVERPROPERTY('EngineEdition'): Personal or Desktop, Standard, Enterprise (and Developer), Express.
SUPPORTED_ENGINE_EDITIONS = {1, 2, 3, 4}

# A plain login holds these on the server; anything more is more than read-only needs.
SERVER_PERMISSIONS_ALLOWED = {"CONNECT SQL", "VIEW ANY DATABASE"}
# msdb roles that can create or run jobs (spec section 3: no fixed role, SQLAgentReaderRole included).
MSDB_ROLES = ("SQLAgentUserRole", "SQLAgentReaderRole", "SQLAgentOperatorRole", "db_owner", "db_datawriter",
              "db_ddladmin")
# Effective permissions on client objects, whether granted to the login, a role or public.
# Sequences are included because UPDATE on one lets NEXT VALUE FOR advance it.
EFFECTIVE_OBJECT_PERMISSIONS = """
SELECT QUOTENAME(s.name) + '.' + QUOTENAME(o.name) AS object_name, p.permission_name,
       CASE WHEN t.temporal_type = 1 THEN 1 ELSE 0 END AS is_history_table
FROM sys.objects AS o
JOIN sys.schemas AS s ON s.schema_id = o.schema_id
LEFT JOIN sys.tables AS t ON t.object_id = o.object_id
CROSS APPLY (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'), ('EXECUTE')) AS p (permission_name)
WHERE o.is_ms_shipped = 0 AND o.type IN ('U', 'V', 'P', 'PC', 'FN', 'IF', 'TF', 'FS', 'FT', 'SO', 'AF')
  AND HAS_PERMS_BY_NAME(QUOTENAME(s.name) + '.' + QUOTENAME(o.name), 'OBJECT', p.permission_name) = 1
ORDER BY object_name, p.permission_name"""
LISTED = 20
# SQL Server's permission functions report INSERT on a temporal history table to a login
# granted nothing on it, yet refuse the INSERT (error 13559) while system versioning is on,
# which is the only time a table is a history table. Held only by that report, it is not
# excess; each one is recorded (decided by the owner on issue #39, ADR 0024).
HISTORY_TABLE_EXEMPTION = ("INSERT is reported on a temporal history table, but SQL Server refuses every direct "
                           "write to one while system versioning is on (error 13559)")

# sys.objects type codes, by catalog kind. Constraints and table types' own objects are read
# as part of their table or type; system and internal objects are not the client's.
OBJECT_KINDS = {"U": "table", "V": "view", "P": "procedure", "FN": "scalar_function", "IF": "inline_function",
                "TF": "table_function", "PC": "clr_procedure", "FS": "clr_scalar_function",
                "FT": "clr_table_function", "AF": "clr_aggregate", "SN": "synonym", "SO": "sequence"}
FOLDED_INTO_PARENT = {"C", "D", "F", "PK", "UQ", "EC", "TT", "TR", "TA", "IT", "S"}
MODULE_KINDS = {"view", "procedure", "scalar_function", "inline_function", "table_function", "trigger",
                "database_trigger"}
CLR_KINDS = {"clr_procedure", "clr_scalar_function", "clr_table_function", "clr_aggregate", "clr_trigger"}
SHAPED_KINDS = {"view", "inline_function", "table_function", "clr_table_function"}


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


class UnsupportedServer(Exception):
    """A server this extractor does not extract from. Nothing was read."""


class TimeZoneRequired(Exception):
    """CURRENT_TIMEZONE() failed, as it does on SQL Server 2017, and no time zone was given."""


# ---------------------------------------------------------------------------- the self-check

def _db_permissions_allowed(permission: str) -> bool:
    # CONNECT and the VIEW ... DEFINITION family that comes with VIEW DEFINITION (Annex F, check 1).
    return permission == "CONNECT" or (permission.startswith("VIEW ") and permission.endswith(" DEFINITION"))


def _capped(items: list[str]) -> list[str]:
    return items if len(items) <= LISTED else items[:LISTED] + [f"... and {len(items) - LISTED} more"]


class _Session:
    """The extractor's connections, opened as it needs them, with every batch they send."""

    def __init__(self, address: ServerAddress):
        self.address = address
        self.statements: list[str] = []

    def open(self, database: str = "master") -> Connection:
        a = self.address
        conn = Connection(a.host, a.port, a.login, a.password, database)
        conn.statements = self.statements
        return conn


def check_permissions(address: ServerAddress, databases: list[str],
                      session: _Session | None = None) -> dict[str, list[dict]]:
    """The self-check (spec section 3, promise 6). Raises ExtractorPermissionError listing
    everything missing and everything held beyond the read-only grant set. Returns, by
    database, every permission reported but not counted as excess, with the reason."""
    session = session or _Session(address)
    exempted: dict[str, list[dict]] = {}
    missing: list[str] = []
    excess: list[str] = []
    with session.open() as master:
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
        with session.open("msdb") as msdb:
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
        with session.open(database) as conn:
            held = {r["permission_name"] for r in conn.query(
                "SELECT permission_name FROM fn_my_permissions(NULL, 'DATABASE')")}
            if "VIEW DEFINITION" not in held:
                missing.append(f"database {database}: VIEW DEFINITION")
            excess += [f"database {database}: {p}" for p in sorted(held) if not _db_permissions_allowed(p)]
            # Reading rows, writing or executing any client object, however it was granted.
            held_on_objects = conn.query(EFFECTIVE_OBJECT_PERMISSIONS)
            exempt = [r for r in held_on_objects if r["is_history_table"] == 1 and r["permission_name"] == "INSERT"]
            exempted[database] = [{"object": r["object_name"], "permission": "INSERT",
                                   "reason": HISTORY_TABLE_EXEMPTION} for r in exempt]
            excess += _capped([f"database {database}: {r['permission_name']} on {r['object_name']}"
                               for r in held_on_objects if r not in exempt])
    if missing or excess:
        raise ExtractorPermissionError(address.login, missing, excess)
    return exempted


# ---------------------------------------------------------------------------- reading

def _json(value: Any) -> Any:
    """A metadata value as JSON: binary as 0x-hex, exact numbers as int where integral, else text."""
    if isinstance(value, bytes):
        return "0x" + value.hex().upper()
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    return value


def _canonical(row: dict) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)


@dataclasses.dataclass
class Capture:
    """One metadata view read wholesale: its columns in order and every row."""
    view: str
    columns: list[str]
    rows: list[dict]


def capture(conn: Connection, view: str) -> Capture:
    columns, rows = conn.capture(f"SELECT * FROM {view}{ROW_FILTERS.get(view, '')}")
    rows = sorted(({k: _json(v) for k, v in r.items()} for r in rows), key=_canonical)
    return Capture(view, columns, rows)


def _marked(fact: str, spans, row: int | None = None, column: str | None = None) -> list[dict]:
    """Marked spans as a `secrets` fact holds them: which fact, and where in it."""
    return [{"fact": fact, **(dataclasses.replace(s, row=row, column=column) if row is not None else s).to_json()}
            for s in spans]


def raw_identity(scope: str, view: str) -> str:
    return f"raw:{scope}/{view}"


def _raw_object(scope: str, cap: Capture) -> CatalogObject:
    read = Provenance("server", cap.view)
    command = frozenset({"command"}) if cap.view == "dbo.sysjobsteps" else frozenset()
    marked = secrets.in_rows(cap.view, cap.rows, command_columns=command)
    facts = [Fact("skeleton", "view", cap.view, read),
             Fact("skeleton", "columns", cap.columns, read),
             Fact("skeleton", "rows", cap.rows, read),
             Fact("skeleton", "secrets", [s.to_json() for s in marked], Provenance("server", f"{cap.view}, marked by "
                                                                                            "the extractor's patterns"))]
    ignored = [c for c in IGNORED_FOR_IDENTITY.get(cap.view, ()) if c in cap.columns]
    if ignored:
        facts.append(Fact("skeleton", IGNORED_FACT, [{"fact": "rows", "columns": ignored}],
                          Provenance("server", "columns that change with nobody acting (ADR 0023, amended)")))
    return CatalogObject(raw_identity(scope, cap.view), "raw_capture", tuple(facts))


@dataclasses.dataclass
class Reading:
    """Everything one extraction read from the server, before any fact is built from it."""
    databases: list[str]
    properties: dict
    server: dict[str, Capture]
    time_zone: str | None                 # from CURRENT_TIMEZONE(), or None where it failed
    time_zone_failure: str | None
    database_captures: dict[str, dict[str, Capture]]
    dependencies_granted: dict[str, bool]
    exempted: dict[str, list[dict]]
    msdb: dict[str, Capture]


@dataclasses.dataclass(frozen=True)
class Settings:
    """What a server would have supplied, given by the operator for databases read from
    folders alone (spec section 3). The time zone is passed as `time_zone`."""
    collations: dict[str, str] = dataclasses.field(default_factory=dict)   # required: each database's default
    version: str | None = None                                             # a release, such as "2019"
    compatibility_levels: dict[str, int] = dataclasses.field(default_factory=dict)


class SettingsRequired(Exception):
    """A setting a server would have supplied is missing, for an extraction without one."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__("Extraction from folders alone needs settings a server would have supplied (spec section 3); "
                         "nothing was read:\n" + "\n".join(f"  missing: {m}" for m in missing))


def extract(address: ServerAddress | None, databases: list[str], *, time_zone: str | None = None,
            statements: list[str] | None = None, folder_inputs: tuple[folders.FolderInput, ...] = (),
            settings: Settings = Settings()) -> Snapshot:
    """Read a live server, folders of files, or both into a snapshot. With a server: check
    its version and the login's permissions, then read the catalog of the named databases,
    msdb's jobs and the server; `time_zone` is required only where CURRENT_TIMEZONE() fails,
    and every batch sent is appended to `statements`. Without one, `databases` is empty, and
    the folders' databases need the settings and `time_zone`."""
    if address is None:
        _require(folder_inputs, settings, time_zone)
    files = folders.read_files(list(folder_inputs))
    reading = read(address, databases, statements=statements) if address is not None else None
    return build(reading, time_zone=time_zone, files=files, folder_inputs=folder_inputs, settings=settings)


def _require(folder_inputs, settings: Settings, time_zone: str | None) -> None:
    missing = [] if time_zone else ["the time zone, which a server would have supplied"]
    missing += [f"the default collation of database {d}" for d in _folder_databases(folder_inputs)
                if not _setting(settings.collations, d)]
    if not folder_inputs:
        missing.append("a folder of files, or a live server")
    if missing:
        raise SettingsRequired(missing)


def _folder_databases(folder_inputs) -> list[str]:
    out: list[str] = []
    for f in folder_inputs:
        if f.database.casefold() not in {d.casefold() for d in out}:
            out.append(f.database)
    return out


def _setting(values: dict, database: str):
    return next((v for k, v in values.items() if k.casefold() == database.casefold()), None)


def read(address: ServerAddress, databases: list[str], *, statements: list[str] | None = None) -> Reading:
    """Everything the extractor sends to the server: the version check, the self-check and
    the wholesale reads. Nothing is built here."""
    session = _Session(address)
    if statements is not None:
        session.statements = statements
    with session.open() as master:
        props = _server_properties(master)
        check_version(props)
    exempted = check_permissions(address, databases, session)
    with session.open() as master:
        server_caps = {v: capture(master, v) for v in SERVER_VIEWS}
        try:
            zone, failure = master.query("SELECT CURRENT_TIMEZONE() AS zone")[0]["zone"], None
        except SqlServerError as failed:
            zone, failure = None, str(failed)
    caps, granted = {}, {}
    for database in databases:
        with session.open(database) as conn:
            granted[database] = conn.query("SELECT HAS_PERMS_BY_NAME(@view, 'OBJECT', 'SELECT') AS held",
                                           {"view": DEPENDENCY_VIEW})[0]["held"] == 1
            views = DATABASE_VIEWS + ((DEPENDENCY_VIEW,) if granted[database] else ())
            caps[database] = {v: capture(conn, v) for v in views}
    with session.open("msdb") as msdb:
        msdb_caps = {v: capture(msdb, v) for v in MSDB_VIEWS}
    return Reading(list(databases), props, server_caps, zone, failure, caps, granted, exempted, msdb_caps)


def build(reading: Reading | None, *, time_zone: str | None = None, files: Sequence[folders.SourceFile] = (),
          folder_inputs: tuple[folders.FolderInput, ...] = (), settings: Settings = Settings()) -> Snapshot:
    """The snapshot, built from what was read alone: no server is touched."""
    objects: list[CatalogObject] = []
    pending: list[_Database] = []
    contexts: dict[str, folders.DatabaseContext] = {}
    if reading is not None:
        objects += [_server_object(reading, time_zone)]
        objects += [_raw_object("server", c) for c in reading.server.values()]
        objects += [_raw_object("db:msdb", c) for c in reading.msdb.values()]
        objects += _agent_jobs(reading.msdb)
        pending = [_Database(name, reading.database_captures[name], reading.dependencies_granted[name],
                             reading.exempted.get(name, [])) for name in reading.databases]
        for db in pending:
            objects += db.objects()
            objects += [_raw_object(f"db:{db.name}", c) for c in db.caps.values()]
            contexts[db.name] = folders.DatabaseContext(db.name, db.collation, db.compatibility_level, True,
                                                        frozenset(db.system_types()), frozenset(db.schemas.values()))
        major = str(reading.properties["ProductMajorVersion"])
        outside = [d for d in _folder_databases(folder_inputs) if _setting({c: c for c in contexts}, d) is None]
        if outside:
            raise folders.FolderExtractionError(
                f"folders are given for {', '.join(outside)}, which this extraction does not read from the live "
                "server: extract those databases from the server too, or from folders alone in an extraction of "
                "their own")
    else:
        major = _folder_version(settings)
        server, database_objects = _folder_server(settings, time_zone, major, _folder_databases(folder_inputs))
        objects += [server, *database_objects]
        for db in database_objects:
            contexts[db.identity] = folders.DatabaseContext(db.identity, db.fact("collation").value,
                                                            db.fact("compatibility_level", SOURCE).value, False)
    found = folders.extract(list(files), contexts, live=reading is not None) if files else None
    if found is not None:
        objects += found.objects
        for name, schemas in folders.schemas_of(found.objects, found.schemas).items():
            if name in contexts and not contexts[name].live:
                contexts[name] = dataclasses.replace(contexts[name], schemas=schemas)
    # References need every object, in every database, to resolve against.
    provisional = Snapshot.of(SOURCE, objects)
    engagement = set(contexts)
    extra: dict[str, list[Fact]] = {}
    for db in pending:
        for identity, facts in db.references(provisional, engagement).items():
            extra.setdefault(identity, []).extend(facts)
    for identity, resolve in (found.pending if found is not None else []):
        extra.setdefault(identity, []).extend(resolve(provisional))
    for identity, facts in _job_references(provisional, contexts, folders.LEVELS[major]).items():
        extra.setdefault(identity, []).extend(facts)
    objects = [dataclasses.replace(o, facts=o.facts + tuple(extra.get(o.identity, ()))) for o in objects]
    return Snapshot.of(SOURCE, objects)


def _job_references(snapshot: Snapshot, contexts: dict[str, folders.DatabaseContext],
                    default_level: int) -> dict[str, list[Fact]]:
    """Each SQL Agent job's references, from its steps."""
    packages: dict[str, list[str]] = {}
    for o in snapshot.objects:
        if o.kind == "ssis_package":
            packages.setdefault(jobs.package_stem(o.fact("file").value), []).append(o.identity)
    steps = {name: c.step() for name, c in contexts.items()}
    out = {}
    for o in snapshot.objects:
        if o.kind != "agent_job":
            continue
        found, unavailable = jobs.step_references(o.fact("steps").value, snapshot, steps, default_level=default_level,
                                                  system_types=folders.SYSTEM_TYPE_NAMES, packages=packages,
                                                  engagement=set(contexts), job=o.identity)
        source = o.fact("steps").provenance
        p = Provenance(source.kind, f"{source.detail}: the steps' commands, parsed by ScriptDom")
        out[o.identity] = [Fact("skeleton", "references", found, p),
                           Fact("skeleton", "references_unavailable", unavailable, p)]
    return out


def _folder_version(settings: Settings) -> str:
    if settings.version is None:
        return folders.VERSION_DEFAULT
    major = next((m for m, release in folders.RELEASES.items() if settings.version in (m, release)), None)
    if major is None:
        raise UnsupportedServer(f"SQL Server {settings.version}: extraction from folders takes a release of "
                                f"{', '.join(folders.RELEASES.values())} (spec section 16)")
    return major


def _folder_server(settings: Settings, time_zone: str | None, major: str,
                   databases: list[str]) -> tuple[CatalogObject, list[CatalogObject]]:
    """The server and database objects of an extraction without a server: every fact is a
    setting or SQL Server's documented default, so every one is assumed."""
    release = folders.RELEASES[major]
    version = Provenance("assumed", f"the version setting: SQL Server {release}" if settings.version else
                         f"no version given: SQL Server {release}, the release the sandbox runs")
    unknown = Provenance("assumed", "no server: not known")
    first = min(databases, key=str.casefold)   # whatever order the folders were given in
    server = CatalogObject(folders.NO_SERVER, "server", (
        Fact("skeleton", "version", f"{major}.0", version),
        Fact("skeleton", "edition", None, unknown),
        Fact("skeleton", "collation", _setting(settings.collations, first),
             Provenance("assumed", f"no server: the default collation of database {first}, a setting")),
        Fact(SOURCE, "build", {"level": None, "update_level": None, "build": None, "release": release}, version),
        Fact(SOURCE, "engine_edition", None, unknown),
        Fact(SOURCE, "nested_triggers", True, Provenance("assumed", "SQL Server's default: nested triggers on")),
        Fact("skeleton", "time_zone", time_zone, Provenance("assumed", "the time zone setting")),
    ))
    out = []
    for name in databases:
        given = _setting(settings.compatibility_levels, name)
        level = int(given or folders.LEVELS[major])
        if level not in parser.PARSERS:
            raise UnsupportedServer(f"compatibility level {level} for database {name} is not one SQL Server has")
        default = Provenance("assumed", "SQL Server's default for a new database")
        ansi = dict.fromkeys(("ansi_nulls", "ansi_padding", "ansi_warnings", "arithabort", "concat_null_yields_null",
                              "numeric_roundabort", "quoted_identifier", "ansi_null_default"), False)
        out.append(CatalogObject(name, "database", (
            Fact("skeleton", "collation", _setting(settings.collations, name),
                 Provenance("assumed", "the database default collation setting")),
            Fact(SOURCE, "compatibility_level", level, Provenance(
                "assumed", "the compatibility level setting" if given else
                f"no level given: {level}, the default for a database created on SQL Server {release}")),
            Fact(SOURCE, "ansi_nulls_default", False, default),
            Fact(SOURCE, "quoted_identifier_default", False, default),
            Fact(SOURCE, "ansi_defaults", ansi, default),
            Fact(SOURCE, "recursive_triggers", False, default),
            Fact(SOURCE, "dependency_cross_check", {"status": "skipped", "reason": "no live server: SQL Server's "
                                                                                  "dependency metadata is not available"},
                 Provenance("assumed", "no live server")),
            Fact(SOURCE, "permission_exemptions", [], Provenance("assumed", "no live server: no self-check ran")),
        )))
    return server, out


def _server_properties(conn: Connection) -> dict:
    columns = ", ".join(f"CAST(SERVERPROPERTY('{p}') AS nvarchar(128)) AS [{p}]" for p in SERVER_PROPERTIES)
    return conn.query(f"SELECT {columns}")[0]


def check_version(props: dict) -> None:
    edition = int(props["EngineEdition"] or 0)
    if edition not in SUPPORTED_ENGINE_EDITIONS:
        raise UnsupportedServer(f"{props['Edition']} (engine edition {edition}) is not supported in v1: only SQL "
                                f"Server itself is (spec section 16). Nothing was read.")
    major = int(str(props["ProductVersion"]).split(".")[0])
    if major not in SUPPORTED_MAJOR_VERSIONS:
        when = "before 2017 is not built yet (issue #74)" if major < 14 else "is not a listed version"
        raise UnsupportedServer(f"SQL Server {props['ProductVersion']}: extraction from a version {when} "
                                f"(spec section 16). Nothing was read.")


def _server_object(reading: Reading, time_zone: str | None) -> CatalogObject:
    props, configurations = reading.properties, reading.server["sys.configurations"]
    sp = Provenance("server", "SERVERPROPERTY")
    nested = [r for r in configurations.rows if r["name"] == "nested triggers"]
    facts = [Fact("skeleton", "version", props["ProductVersion"], sp),
             Fact("skeleton", "edition", props["Edition"], sp),
             Fact("skeleton", "collation", props["Collation"], sp),
             Fact(SOURCE, "build", {"level": props["ProductLevel"], "update_level": props["ProductUpdateLevel"],
                                    "build": props["ProductBuild"],
                                    "release": SUPPORTED_MAJOR_VERSIONS[int(props["ProductMajorVersion"])]}, sp),
             Fact(SOURCE, "engine_edition", int(props["EngineEdition"]), sp),
             Fact(SOURCE, "nested_triggers", bool(int(nested[0]["value_in_use"])),
                  Provenance("server", "sys.configurations"))]
    if reading.time_zone is not None:
        facts.append(Fact("skeleton", "time_zone", reading.time_zone, Provenance("server", "CURRENT_TIMEZONE()")))
    else:
        failed = reading.time_zone_failure
        if time_zone is None:
            raise TimeZoneRequired(f"CURRENT_TIMEZONE() failed on SQL Server {props['ProductVersion']} ({failed}); "
                                   "give the server's time zone as a setting (spec section 3).") from None
        facts.append(Fact("skeleton", "time_zone", time_zone,
                          Provenance("assumed", f"time zone setting; CURRENT_TIMEZONE() failed: {failed}")))
    return CatalogObject(props["ServerName"], "server", tuple(facts))


# ---------------------------------------------------------------------------- one database

def _type_shape(row: dict, types: dict[int, dict]) -> dict:
    """A column's or parameter's declared type, from its sys.columns or sys.parameters row."""
    declared = types[row["user_type_id"]]
    # Table types (243) and CLR types (240: hierarchyid, geometry, geography and user CLR
    # types) have no sys.types row for their system type.
    if declared["is_table_type"]:
        system = "table"
    elif declared["is_assembly_type"]:
        system = "clr_type" if declared["is_user_defined"] else declared["name"]
    elif row["system_type_id"] in types:
        system = types[row["system_type_id"]]["name"]
    else:
        raise KeyError(f"system type {row['system_type_id']} of type {declared['name']} is not in sys.types")
    return {"declared_type": declared["name"], "system_type": system,
            "max_length": int(row["max_length"]), "precision": int(row["precision"]), "scale": int(row["scale"])}


class _Database:
    """The catalog objects of one database, built from its raw capture."""

    def __init__(self, name: str, caps: dict[str, Capture], dependencies_granted: bool, exempted: list[dict]):
        self.name, self.caps, self.dependencies_granted = name, caps, dependencies_granted
        self.exempted = exempted
        rows = {view: cap.rows for view, cap in caps.items()}
        self.rows = rows
        self.schemas = {r["schema_id"]: r["name"] for r in rows["sys.schemas"]}
        self.types = {r["user_type_id"]: r for r in rows["sys.types"]}
        self.objects_by_id = {r["object_id"]: r for r in rows["sys.objects"]}
        self.columns: dict[int, list[dict]] = {}
        for r in sorted(rows["sys.columns"], key=lambda r: (r["object_id"], r["column_id"])):
            self.columns.setdefault(r["object_id"], []).append(r)
        self.modules = {r["object_id"]: r for r in rows["sys.sql_modules"]}
        self.parameters: dict[int, list[dict]] = {}
        for r in sorted(rows["sys.parameters"], key=lambda r: (r["object_id"], r["parameter_id"])):
            self.parameters.setdefault(r["object_id"], []).append(r)
        db = rows["sys.databases"][0]
        self.compatibility_level = int(db["compatibility_level"])
        self.collation = db["collation_name"]
        self.identities: dict[int, str] = {}     # object_id -> identity, for every object recorded
        self.type_identities: dict[int, str] = {}
        self.module_kinds: dict[str, tuple[int, str]] = {}   # identity -> (object_id, kind)

    def identity(self, schema_id: int, name: str) -> str:
        return f"{self.name}.{self.schemas[schema_id]}.{name}"

    def name_parts(self, schema_id: int, name: str) -> Fact:
        """The name's parts, kept apart because a part may itself hold a period."""
        return Fact("skeleton", "name_parts", [self.name, self.schemas[schema_id], name],
                    Provenance("server", "sys.objects, sys.schemas"))

    def objects(self) -> list[CatalogObject]:
        out = [self.database_object()]
        for r in sorted(self.rows["sys.objects"], key=lambda r: r["object_id"]):
            code = str(r["type"]).strip()
            if r["is_ms_shipped"] or r["parent_object_id"] != 0 or code in FOLDED_INTO_PARENT:
                continue
            kind = OBJECT_KINDS.get(code, "other_object")
            identity = self.identity(r["schema_id"], r["name"])
            self.identities[r["object_id"]] = identity
            out.append(self.schema_object(identity, kind, r))
        out += self.triggers()
        out += self.user_types()
        return out

    # ------------------------------------------------------------------ the database

    def database_object(self) -> CatalogObject:
        db = self.rows["sys.databases"][0]
        p = Provenance("server", "sys.databases")
        ansi = {key: bool(db[f"is_{key}_on"]) for key in (
            "ansi_nulls", "ansi_padding", "ansi_warnings", "arithabort", "concat_null_yields_null",
            "numeric_roundabort", "quoted_identifier", "ansi_null_default")}
        check = ({"status": "run"} if self.dependencies_granted else
                 {"status": "skipped", "reason": f"the login holds no SELECT on {DEPENDENCY_VIEW}, which Annex F "
                                                 "grants as optional"})
        return CatalogObject(self.name, "database", (
            Fact("skeleton", "collation", db["collation_name"], p),
            Fact(SOURCE, "compatibility_level", self.compatibility_level, p),
            Fact(SOURCE, "ansi_nulls_default", ansi["ansi_nulls"], p),
            Fact(SOURCE, "quoted_identifier_default", ansi["quoted_identifier"], p),
            Fact(SOURCE, "ansi_defaults", ansi, p),
            Fact(SOURCE, "recursive_triggers", bool(db["is_recursive_triggers_on"]), p),
            Fact(SOURCE, "dependency_cross_check", check, Provenance("server", "HAS_PERMS_BY_NAME")),
            Fact(SOURCE, "permission_exemptions", self.exempted, Provenance("server", "the extractor's self-check")),
        ))

    # ------------------------------------------------------------------ schema objects

    def schema_object(self, identity: str, kind: str, row: dict) -> CatalogObject:
        oid = row["object_id"]
        facts = [Fact(SOURCE, "object_type", str(row["type"]).strip(), Provenance("server", "sys.objects")),
                 self.name_parts(row["schema_id"], row["name"])]
        if kind in ("table", "view"):
            facts += self.relation_facts(oid, kind)
        if kind in MODULE_KINDS or kind in CLR_KINDS:
            facts += self.module_facts(identity, oid, kind)
        if kind in SHAPED_KINDS:
            facts.append(Fact("skeleton", "result_shape", self.shape(oid), Provenance("server", "sys.columns")))
        if kind in ("procedure", "scalar_function", "inline_function", "table_function") or kind in CLR_KINDS:
            facts += self.parameter_facts(oid, kind)
        if kind == "synonym":
            [syn] = [s for s in self.rows["sys.synonyms"] if s["object_id"] == oid]
            facts.append(Fact("skeleton", "base_object", list(split_name(syn["base_object_name"])),
                              Provenance("server", "sys.synonyms")))
        if kind == "sequence":
            [seq] = [s for s in self.rows["sys.sequences"] if s["object_id"] == oid]
            facts.append(Fact("skeleton", "sequence", {
                "type": self.types[seq["user_type_id"]]["name"], "start": seq["start_value"],
                "increment": seq["increment"], "minimum": seq["minimum_value"], "maximum": seq["maximum_value"],
                "cycles": bool(seq["is_cycling"]), "cached": bool(seq["is_cached"]),
                "cache_size": seq["cache_size"]}, Provenance("server", "sys.sequences")))
        if kind == "other_object":
            facts.append(Fact(SOURCE, "object_type_description", row["type_desc"], Provenance("server", "sys.objects")))
        return CatalogObject(identity, kind, tuple(facts))

    def shape(self, oid: int) -> list[dict]:
        return [{"name": c["name"], **_type_shape(c, self.types), "nullable": bool(c["is_nullable"])}
                for c in self.columns.get(oid, [])]

    def relation_facts(self, oid: int, kind: str) -> list[Fact]:
        rows = self.rows
        facts: list[Fact] = []
        if kind == "table":
            defaults = {(d["parent_object_id"], d["parent_column_id"]): d for d in rows["sys.default_constraints"]}
            identity = {(i["object_id"], i["column_id"]): i for i in rows["sys.identity_columns"]}
            computed = {(c["object_id"], c["column_id"]): c for c in rows["sys.computed_columns"]}
            columns = []
            for c in self.columns.get(oid, []):
                key = (oid, c["column_id"])
                declared = self.types[c["user_type_id"]]
                column = {"name": c["name"], **_type_shape(c, self.types), "nullable": bool(c["is_nullable"]),
                          "collation": c["collation_name"], "identity": bool(c["is_identity"]),
                          "computed": bool(c["is_computed"]),
                          "user_type": (names.type_identity(self.name, self.schemas[declared["schema_id"]],
                                                            declared["name"]) if declared["is_user_defined"] else None),
                          "default": ({"name": defaults[key]["name"], "definition": defaults[key]["definition"]}
                                      if key in defaults else None)}
                if key in identity:
                    column["identity_seed"] = identity[key]["seed_value"]
                    column["identity_increment"] = identity[key]["increment_value"]
                if key in computed:
                    column["computed_expression"] = computed[key]["definition"]
                    column["computed_persisted"] = bool(computed[key]["is_persisted"])
                columns.append(column)
            detail = ("sys.columns, sys.types, sys.default_constraints, sys.identity_columns, sys.computed_columns")
            facts.append(Fact("skeleton", "columns", columns, Provenance("server", detail)))
            facts.append(Fact("skeleton", "not_null", [c["name"] for c in columns if not c["nullable"]],
                              Provenance("server", "sys.columns")))
            count = sum(int(p["rows"]) for p in rows["sys.partitions"] if p["object_id"] == oid and p["index_id"] in (0, 1))
            facts.append(Fact("skeleton", "row_count", count, Provenance("server", "sys.partitions (metadata, no scan)")))
            facts.append(Fact("skeleton", IGNORED_FACT, [{"fact": f} for f in INFORMATIONAL_FACTS["table"]],
                              Provenance("server", "volatile informational facts, for the report alone "
                                                   "(ADR 0023, amended)")))
            facts += self.constraint_facts(oid)
            facts.append(self.secrets_in_expressions(columns, oid))
        else:
            facts += [f for f in self.constraint_facts(oid) if f.name == "unique_indexes"]
        return facts

    def index_columns(self, oid: int, index_id: int) -> tuple[list[dict], list[str]]:
        names_by_id = {c["column_id"]: c["name"] for c in self.columns.get(oid, [])}
        rows = sorted((r for r in self.rows["sys.index_columns"] if r["object_id"] == oid and r["index_id"] == index_id),
                      key=lambda r: (r["key_ordinal"], r["index_column_id"]))
        keys = [{"name": names_by_id[r["column_id"]], "descending": bool(r["is_descending_key"])}
                for r in rows if r["key_ordinal"] > 0]
        included = [names_by_id[r["column_id"]] for r in rows if r["key_ordinal"] == 0 and r["is_included_column"]]
        return keys, included

    def constraint_facts(self, oid: int) -> list[Fact]:
        rows = self.rows
        names_by_id = {c["column_id"]: c["name"] for c in self.columns.get(oid, [])}
        keys = sorted((k for k in rows["sys.key_constraints"] if k["parent_object_id"] == oid), key=lambda k: k["name"])
        indexes = {i["index_id"]: i for i in rows["sys.indexes"] if i["object_id"] == oid}
        pk, unique = None, []
        for k in keys:
            columns, _ = self.index_columns(oid, k["unique_index_id"])
            entry = {"name": k["name"], "columns": columns, "index_type": indexes[k["unique_index_id"]]["type_desc"]}
            if str(k["type"]).strip() == "PK":
                pk = entry
            else:
                unique.append(entry)
        checks = [{"name": c["name"], "definition": c["definition"],
                   "column": names_by_id.get(c["parent_column_id"]) if c["parent_column_id"] else None,
                   "enabled": not c["is_disabled"], "trusted": not c["is_not_trusted"]}
                  for c in sorted((c for c in rows["sys.check_constraints"] if c["parent_object_id"] == oid),
                                  key=lambda c: c["name"])]
        fks = []
        for f in sorted((f for f in rows["sys.foreign_keys"] if f["parent_object_id"] == oid), key=lambda f: f["name"]):
            cols = sorted((c for c in rows["sys.foreign_key_columns"] if c["constraint_object_id"] == f["object_id"]),
                          key=lambda c: c["constraint_column_id"])
            ref = f["referenced_object_id"]
            ref_row = self.objects_by_id[ref]
            ref_names = {c["column_id"]: c["name"] for c in self.columns.get(ref, [])}
            fks.append({"name": f["name"], "columns": [names_by_id[c["parent_column_id"]] for c in cols],
                        "references": self.identity(ref_row["schema_id"], ref_row["name"]),
                        "referenced_columns": [ref_names[c["referenced_column_id"]] for c in cols],
                        "on_delete": f["delete_referential_action_desc"], "on_update": f["update_referential_action_desc"],
                        "enabled": not f["is_disabled"], "trusted": not f["is_not_trusted"]})
        unique_indexes = []
        for i in sorted(indexes.values(), key=lambda i: i["index_id"]):
            if i["is_unique"] and not i["is_primary_key"] and not i["is_unique_constraint"]:
                columns, included = self.index_columns(oid, i["index_id"])
                unique_indexes.append({"name": i["name"], "columns": columns, "included": included,
                                       "filter": i["filter_definition"] if i["has_filter"] else None,
                                       "index_type": i["type_desc"], "enabled": not i["is_disabled"]})
        return [Fact("skeleton", "primary_key", pk, Provenance("server", "sys.key_constraints, sys.index_columns")),
                Fact("skeleton", "unique_constraints", unique,
                     Provenance("server", "sys.key_constraints, sys.index_columns")),
                Fact("skeleton", "check_constraints", checks, Provenance("server", "sys.check_constraints")),
                Fact("skeleton", "foreign_keys", fks, Provenance("server", "sys.foreign_keys, sys.foreign_key_columns")),
                Fact("skeleton", "unique_indexes", unique_indexes, Provenance("server", "sys.indexes, sys.index_columns"))]

    def secrets_in_expressions(self, columns: list[dict], oid: int) -> Fact:
        """Likely secrets in a table's expressions: defaults, computed columns and checks."""
        marked = []
        for index, c in enumerate(columns):
            for key, text in (("default", (c["default"] or {}).get("definition")),
                              ("computed_expression", c.get("computed_expression"))):
                marked += _marked("columns", secrets.in_text(text or ""), index, key)
        checks = sorted((c for c in self.rows["sys.check_constraints"] if c["parent_object_id"] == oid),
                        key=lambda c: c["name"])
        for index, c in enumerate(checks):
            marked += _marked("check_constraints", secrets.in_text(c["definition"] or ""), index, "definition")
        return Fact("skeleton", "secrets", marked, Provenance("server", "table expressions, marked by the extractor's "
                                                                        "patterns"))

    # ------------------------------------------------------------------ modules

    def module_facts(self, identity: str, oid: int, kind: str) -> list[Fact]:
        p = Provenance("server", "sys.sql_modules")
        module = self.modules.get(oid)
        if kind in CLR_KINDS or module is None:
            reason = ("a CLR module: it has no T-SQL source" if kind in CLR_KINDS else
                      "not in sys.sql_modules though VIEW DEFINITION is held")
            return [Fact("skeleton", "source_text", None, p), Fact("skeleton", "extractable", False, p),
                    Fact("skeleton", "not_extractable_reason", reason, p)]
        text = module["definition"]
        facts = [Fact(SOURCE, "ansi_nulls", bool(module["uses_ansi_nulls"]), p),
                 Fact(SOURCE, "quoted_identifier", bool(module["uses_quoted_identifier"]), p),
                 Fact(SOURCE, "schema_bound", bool(module["is_schema_bound"]), p)]
        if text is None:
            # The self-check proved VIEW DEFINITION is held, so a NULL definition is encryption.
            facts += [Fact("skeleton", "source_text", None, p), Fact("skeleton", "extractable", False, p),
                      Fact("skeleton", "not_extractable_reason",
                           "encrypted (WITH ENCRYPTION): recorded as present, manual work", p)]
            return facts
        self.module_kinds[identity] = (oid, kind)
        marked = _marked("source_text", secrets.in_text(text))
        return facts + [Fact("skeleton", "source_text", text, p), Fact("skeleton", "extractable", True, p),
                        Fact("skeleton", "secrets", marked, Provenance("server", "sys.sql_modules, marked by the "
                                                                                 "extractor's patterns"))]

    def parameter_facts(self, oid: int, kind: str) -> list[Fact]:
        params, returns = [], None
        defaults, unknown = ({}, None) if kind in CLR_KINDS else self.parsed_defaults(oid)
        for r in self.parameters.get(oid, []):
            shape = _type_shape(r, self.types)
            if r["parameter_id"] == 0:
                returns = shape
                continue
            param = {"name": r["name"], **shape, "direction": "output" if r["is_output"] else "input",
                     "readonly": bool(r["is_readonly"]), "nullable": bool(r["is_nullable"])}
            if kind in CLR_KINDS:
                param["default"] = str(r["default_value"]) if r["has_default_value"] else None
            elif unknown is not None:
                param["default_unknown"] = unknown   # no "default" key: reading it fails loudly
            else:
                param["default"] = defaults.get(str(r["name"]).casefold())
            params.append(param)
        detail = "sys.parameters, sys.types; defaults parsed from sys.sql_modules.definition"
        facts = [Fact("skeleton", "parameters", params, Provenance("server", detail))]
        if kind in ("scalar_function", "clr_scalar_function"):
            facts.append(Fact("skeleton", "returns", returns, Provenance("server", "sys.parameters")))
        return facts

    def parsed(self, oid: int):
        """The module's text parsed at the database's level and the module's QUOTED_IDENTIFIER."""
        module = self.modules.get(oid)
        if module is None or module["definition"] is None:
            return None
        if not hasattr(self, "_parsed"):
            self._parsed: dict[int, Any] = {}
        if oid not in self._parsed:
            try:
                self._parsed[oid] = parser.parse(str(oid), module["definition"], self.compatibility_level,
                                                 bool(module["uses_quoted_identifier"]))
            except parser.ParseFailed as failed:
                self._parsed[oid] = failed
        return self._parsed[oid]

    def parsed_defaults(self, oid: int) -> tuple[dict[str, str | None], str | None]:
        """T-SQL parameter defaults are in the text only (sys.parameters.has_default_value is
        always 0 for them), so they are read from the parsed CREATE statement. Returns the
        defaults by folded name, or, where the text cannot be read, why they are unknown."""
        parsed = self.parsed(oid)
        if parsed is None:
            return {}, "the module's text is not extractable"
        if isinstance(parsed, parser.ParseFailed):
            return {}, f"the module's text did not parse: {parsed}"
        found: dict[str, str | None] = {}
        for batch in parsed.tree.Batches:
            for statement in batch.Statements:
                for p in (getattr(statement, "Parameters", None) or []):
                    value = p.Value
                    found[str(p.VariableName.Value).casefold()] = (
                        None if value is None else parsed.text[int(value.StartOffset):int(value.StartOffset) +
                                                               int(value.FragmentLength)])
        return found, None

    # ------------------------------------------------------------------ triggers and types

    def triggers(self) -> list[CatalogObject]:
        out = []
        events: dict[int, list[dict]] = {}
        for e in self.rows["sys.trigger_events"]:
            events.setdefault(e["object_id"], []).append(e)
        for t in sorted(self.rows["sys.triggers"], key=lambda t: t["object_id"]):
            if t["is_ms_shipped"]:
                continue
            p = Provenance("server", "sys.triggers, sys.trigger_events")
            clr = str(t["type"]).strip() == "TA"
            fired = sorted(events.get(t["object_id"], []), key=lambda e: e["type_desc"])
            order = {e["type_desc"]: "first" if e["is_first"] else "last" if e["is_last"] else "none" for e in fired}
            facts = [Fact("skeleton", "events", [e["type_desc"] for e in fired], p),
                     Fact("skeleton", "timing", "INSTEAD OF" if t["is_instead_of_trigger"] else "AFTER", p),
                     Fact("skeleton", "firing_order", order, p),
                     Fact("skeleton", "enabled", not t["is_disabled"], p)]
            if t["parent_class"] == 1:
                parent = self.objects_by_id[t["parent_id"]]
                own = self.objects_by_id[t["object_id"]]
                identity = self.identity(own["schema_id"], t["name"])
                kind = "clr_trigger" if clr else "trigger"
                facts.append(self.name_parts(own["schema_id"], t["name"]))
                facts.append(Fact("skeleton", "table", self.identity(parent["schema_id"], parent["name"]), p))
            else:
                identity, kind = f"{self.name}.{t['name']}", "database_trigger"
            self.identities[t["object_id"]] = identity
            facts += self.module_facts(identity, t["object_id"], "clr_trigger" if clr else kind)
            out.append(CatalogObject(identity, kind, tuple(facts)))
        return out

    def user_types(self) -> list[CatalogObject]:
        out = []
        table_types = {r["user_type_id"]: r for r in self.rows["sys.table_types"]}
        for t in sorted(self.rows["sys.types"], key=lambda t: t["user_type_id"]):
            if not t["is_user_defined"]:
                continue
            identity = names.type_identity(self.name, self.schemas[t["schema_id"]], t["name"])
            self.type_identities[t["user_type_id"]] = identity
            p = Provenance("server", "sys.types")
            if t["is_table_type"]:
                oid = table_types[t["user_type_id"]]["type_table_object_id"]
                facts = [Fact("skeleton", "columns", [{**c, "collation": r["collation_name"]} for c, r in
                                                      zip(self.shape(oid), self.columns.get(oid, []))],
                              Provenance("server", "sys.table_types, sys.columns"))]
                facts += self.constraint_facts(oid)
                out.append(CatalogObject(identity, "table_type", tuple(facts)))
            elif t["is_assembly_type"]:
                out.append(CatalogObject(identity, "user_type", (Fact("skeleton", "clr_type", True, p),)))
            else:
                out.append(CatalogObject(identity, "user_type", (Fact("skeleton", "base_type", {
                    "system_type": self.types[t["system_type_id"]]["name"], "max_length": int(t["max_length"]),
                    "precision": int(t["precision"]), "scale": int(t["scale"]), "nullable": bool(t["is_nullable"]),
                    "collation": t["collation_name"]}, p),)))
        return out

    # ------------------------------------------------------------------ references

    def context(self, referrer: str | None) -> dict:
        """Where a name written in `referrer` is looked for: its schema, then dbo. A referrer
        that is not a schema object (a database trigger, or none) looks in dbo only."""
        schema = referrer.split(".")[1] if referrer and referrer.count(".") == 2 else "dbo"
        return resolution.context(self.name, schema, names.identifiers_case_sensitive(self.collation))

    def resolve(self, snapshot: Snapshot, referrer: str | None, parts: tuple[str, ...],
                namespace: str) -> tuple[str | None, str | None, str | None]:
        return resolution.resolve(snapshot, parts, namespace, self.context(referrer))

    def system_types(self) -> set[str]:
        return {str(t["name"]).casefold() for t in self.rows["sys.types"] if not t["is_user_defined"]}

    def references(self, snapshot: Snapshot, engagement: set[str]) -> dict[str, list[Fact]]:
        """Each module's references, worked out by the parser and resolved against the whole
        catalog, and the cross-check against sys.sql_expression_dependencies."""
        system_types = self.system_types()
        schemas = {s.casefold() for s in self.schemas.values()}
        server = self.server_dependencies(snapshot)
        out: dict[str, list[Fact]] = {}
        p = Provenance("server", "sys.sql_modules.definition, parsed by ScriptDom")
        for identity, (oid, kind) in sorted(self.module_kinds.items()):
            parsed = self.parsed(oid)
            facts: list[Fact] = []
            if isinstance(parsed, parser.ParseFailed) or parsed is None:
                reason = f"the text did not parse: {parsed}"
                facts.append(Fact("skeleton", "references_unavailable", reason, p))
                check = {"status": "not run", "reason": reason}
            else:
                found = resolution.work_out(parsed, snapshot, self.context(identity), system_types=system_types,
                                            schemas=schemas, engagement=engagement)
                facts.append(Fact("skeleton", "references", found, p))
                check = (self.cross_check(found, server.get(oid, [])) if self.dependencies_granted else
                         {"status": "skipped", "reason": f"the login holds no SELECT on {DEPENDENCY_VIEW}"})
            if self.dependencies_granted:
                facts.append(Fact(SOURCE, "server_dependencies", server.get(oid, []), Provenance("server", DEPENDENCY_VIEW)))
            facts.append(Fact(SOURCE, "dependency_cross_check", check,
                              Provenance("server", f"parser references against {DEPENDENCY_VIEW}")))
            out[identity] = facts
        # Dependencies from objects that are not modules (computed columns, constraints,
        # defaults) are kept, but the parser does not read those expressions yet.
        if self.dependencies_granted:
            unchecked, unattributed = self.unchecked_dependencies(server)
            if unattributed:
                out.setdefault(self.name, []).append(Fact(SOURCE, "unattributed_dependencies", unattributed,
                                                          Provenance("server", DEPENDENCY_VIEW)))
            for parent, deps in sorted(unchecked.items()):
                out.setdefault(parent, []).append(Fact(SOURCE, "server_dependencies", deps,
                                                       Provenance("server", DEPENDENCY_VIEW)))
                out[parent].append(Fact(SOURCE, "dependency_cross_check", {
                    "status": "not run", "reason": "the parser does not read constraint, default and computed-column "
                                                   "expressions yet (issue #42)"},
                    Provenance("server", DEPENDENCY_VIEW)))
        return out

    def server_dependencies(self, snapshot: Snapshot) -> dict[int, list[dict]]:
        """sys.sql_expression_dependencies, by referencing object id, one entry per referenced
        entity, resolved: by its id where SQL Server bound it, else by its name as ours resolves."""
        if not self.dependencies_granted:
            return {}
        by_referrer: dict[int, dict[str, dict]] = {}
        for d in self.rows[DEPENDENCY_VIEW]:
            parts = tuple(p for p in (d["referenced_server_name"], d["referenced_database_name"],
                                      d["referenced_schema_name"], d["referenced_entity_name"]) if p is not None)
            cls = int(d["referenced_class"])
            namespace = "type" if cls == 6 else "object"
            referrer = self.identities.get(d["referencing_id"])
            resolved = None
            if d["referenced_id"] is not None and not d["referenced_server_name"] and not d["referenced_database_name"]:
                resolved = (self.type_identities if cls == 6 else self.identities).get(d["referenced_id"])
            if resolved is None and cls in (1, 6):
                # SQL Server binds a name to a synonym itself, so the synonym is what it names.
                target, via, _ambiguous = self.resolve(snapshot, referrer, parts, namespace)
                resolved = via or target
            entry = {"name": list(parts), "class": d["referenced_class_desc"], "resolved": resolved,
                     "caller_dependent": bool(d["is_caller_dependent"]), "ambiguous": bool(d["is_ambiguous"])}
            by_referrer.setdefault(d["referencing_id"], {})[_canonical(entry)] = entry
        return {oid: sorted(entries.values(), key=_canonical) for oid, entries in by_referrer.items()}

    def unchecked_dependencies(self, server: dict[int, list[dict]]) -> tuple[dict[str, list[dict]], list[dict]]:
        """SQL Server's dependencies from objects that are not parsed modules, by the catalog
        object they belong to; and those whose referencing object is in no catalog object,
        such as one shipped by Microsoft, kept with its id so none is dropped."""
        modules = {oid for oid, _ in self.module_kinds.values()}
        out: dict[str, list[dict]] = {}
        unattributed: list[dict] = []
        for oid, deps in sorted(server.items()):
            if oid in modules:
                continue
            row = self.objects_by_id.get(oid)
            parent = self.identities.get(oid) or (self.identities.get(row["parent_object_id"]) if row else None)
            if parent is None:
                unattributed += [{"referencing_id": oid, **d} for d in deps]
                continue
            out.setdefault(parent, []).extend(deps)
        return {k: sorted(v, key=_canonical) for k, v in out.items()}, unattributed

    @staticmethod
    def cross_check(parsed: list[dict], server: list[dict]) -> dict:
        """Compare the parser's references with SQL Server's. Two kinds of entry are set aside,
        each listed with its reason, because one side never records them: system objects,
        which sys.sql_expression_dependencies does not track, and a trigger's inserted and
        deleted, which it records and which are not catalog objects."""
        def key(entry: dict) -> str:
            # A name written through a synonym names the synonym, as SQL Server records it.
            return entry.get("via") or entry["resolved"] or "name:" + ".".join(p.casefold() for p in entry["name"] if p)
        ignored = sorted({key(r) for r in parsed if r["external"] == "system object"})
        ours = {key(r) for r in parsed if not (r["external"] or "").startswith(("linked server (", "ad hoc"))
                and r["external"] != "system object"}
        pseudo = {key(d) for d in server if d["resolved"] is None and len(d["name"]) == 1
                  and d["name"][0].casefold() in references.PSEUDO_TABLES}
        theirs = {key(d) for d in server} - pseudo
        result = {"status": "agrees" if ours == theirs else "disagrees",
                  "parser_only": sorted(ours - theirs), "server_only": sorted(theirs - ours)}
        if ignored:
            result["ignored_parser"] = {"entries": ignored, "reason": f"system objects, which {DEPENDENCY_VIEW} "
                                                                      "does not track"}
        if pseudo:
            result["ignored_server"] = {"entries": sorted(pseudo), "reason": "a trigger's inserted and deleted "
                                                                             "tables, which are not catalog objects"}
        return result


PROVENANCE_COUNTS = {"server": "read", "file": "parsed", "assumed": "assumed"}
# Objects that hold what was read rather than facts about the client's objects.
NOT_COUNTED = {"raw_capture", "folder_extraction"}


def findings(snapshot: Snapshot) -> dict:
    """What an extraction found that the report must show: the dependency cross-check, by
    database and by object where it disagrees, did not run or was skipped; the marked
    secrets; every permission the self-check saw but did not count as excess; every fact
    assumed rather than read; dependencies SQL Server records from no catalog object; the
    completeness summary (facts read, parsed and assumed, and values assumed inside parsed
    facts); every column whose collation was assumed; and what the folders held."""
    databases, disagreements, not_run, skipped, marked, exempted, assumed, unattributed = {}, [], [], [], [], {}, [], {}
    counts = {"read": 0, "parsed": 0, "assumed": 0}
    assumed_values, collations = 0, []
    for o in snapshot.objects:
        facts = {f.name: f.value for f in o.facts}
        assumed += [{"object": o.identity, "fact": f.name, "assumed": f.provenance.detail}
                    for f in o.facts if f.provenance.kind == "assumed"]
        if o.kind not in NOT_COUNTED:
            for f in o.facts:
                counts[PROVENANCE_COUNTS[f.provenance.kind]] += 1
            for value in facts.get("assumed_values", []):
                assumed_values += 1
                if value["property"] == "collation":
                    collations.append({"object": o.identity, "fact": value["fact"], "column": value.get("column"),
                                       "collation": value["value"], "reason": value["reason"]})
        if o.kind == "database":
            databases[o.identity] = facts["dependency_cross_check"]
            if facts["permission_exemptions"]:
                exempted[o.identity] = facts["permission_exemptions"]
            if facts.get("unattributed_dependencies"):
                unattributed[o.identity] = facts["unattributed_dependencies"]
            continue
        check = facts.get("dependency_cross_check")
        if check and check["status"] == "disagrees":
            disagreements.append({"object": o.identity, "parser_only": check["parser_only"],
                                  "server_only": check["server_only"]})
        elif check and check["status"] == "not run":
            not_run.append({"object": o.identity, "reason": check["reason"]})
        elif check and check["status"] == "skipped":
            skipped.append({"object": o.identity, "reason": check["reason"]})
        if facts.get("secrets"):
            marked.append({"object": o.identity, "spans": len(facts["secrets"])})
    found = {"dependency_cross_check": {"databases": databases, "disagreements": disagreements, "not_run": not_run,
                                        "skipped": skipped, "unattributed": unattributed},
             "secrets": {"marked": marked},
             "permission_exemptions": exempted,
             "assumed": assumed,
             "completeness": {"facts": counts, "assumed_values": assumed_values},
             "assumed_collations": collations}
    if snapshot.has(folders.FOLDERS_IDENTITY):
        found["folders"] = {f.name: f.value for f in snapshot.get(folders.FOLDERS_IDENTITY).facts}
    return found


# ---------------------------------------------------------------------------- recognised artefacts

RECOGNISED = {"agent_job": "SQL Agent job", "ssis_package": "SSIS package"}
INVENTORY = {"agent_job": ("enabled", "description", "category", "start_step", "steps", "schedules",
                           "not_interpreted"),
             "ssis_package": ("file", "package_name", "protection_level", "format_version", "inventoried",
                              "not_inventoried_reason", "connections", "variables", "parameters", "tasks",
                              "components", "precedence_constraints", "sensitive_properties")}
# The cells of an inventory that may hold a marked secret, masked wherever the inventory is shown.
MASKED = {"agent_job": {"steps": ("command",)},
          "ssis_package": {"connections": ("connection_string", "expression"), "tasks": ("sql",),
                           "components": ("sql",), "variables": ("value",), "parameters": ("value",),
                           "sensitive_properties": ("value",)}}


def recognised(snapshot: Snapshot) -> list[dict]:
    """Every SSIS package and SQL Agent job in the snapshot, a recognised artefact type: its
    inventory with every marked secret masked by its named setting, its references to
    catalog objects, the objects they reach (its entry points), and what was not read."""
    out = []
    for o in snapshot.objects:
        if o.kind not in RECOGNISED:
            continue
        marked = o.fact("secrets").value if o.has_fact("secrets") else []
        settings: list[str] = []
        inventory = {}
        for name in INVENTORY[o.kind]:
            if not o.has_fact(name):
                continue
            value = copy.deepcopy(o.fact(name).value)
            for column in MASKED[o.kind].get(name, ()):
                for row, entry in enumerate(value):
                    entry[column], used = core_secrets.mask_cell(entry[column], marked, prefix=o.identity, fact=name,
                                                                 row=row, column=column)
                    settings += [s for s in used if s not in settings]
            inventory[name] = value
        references = o.fact("references").value if o.has_fact("references") else []
        out.append({"name": o.identity, "type": RECOGNISED[o.kind], "inventory": inventory,
                    "references": references,
                    "references_unavailable": (o.fact("references_unavailable").value
                                               if o.has_fact("references_unavailable") else []),
                    "entry_points": sorted({r["resolved"] for r in references if r["resolved"]}),
                    "secrets": settings,
                    "assumed_values": o.fact("assumed_values").value if o.has_fact("assumed_values") else []})
    return out


# ---------------------------------------------------------------------------- msdb

def _agent_jobs(caps: dict[str, Capture]) -> list[CatalogObject]:
    rows = {v: c.rows for v, c in caps.items()}
    categories = {c["category_id"]: c["name"] for c in rows["dbo.syscategories"]}
    schedules = {s["schedule_id"]: s for s in rows["dbo.sysschedules"]}
    out = []
    p = Provenance("server", "msdb: dbo.sysjobs, dbo.sysjobsteps, dbo.sysschedules, dbo.sysjobschedules, "
                             "dbo.syscategories")
    for job in sorted(rows["dbo.sysjobs"], key=lambda j: j["name"]):
        steps = [{"step_id": s["step_id"], "name": s["step_name"], "subsystem": s["subsystem"],
                  "command": s["command"], "database": s["database_name"],
                  "on_success_action": s["on_success_action"], "on_fail_action": s["on_fail_action"],
                  "retry_attempts": s["retry_attempts"]}
                 for s in sorted((s for s in rows["dbo.sysjobsteps"] if s["job_id"] == job["job_id"]),
                                 key=lambda s: s["step_id"])]
        attached = [schedules[a["schedule_id"]] for a in rows["dbo.sysjobschedules"] if a["job_id"] == job["job_id"]]
        job_schedules = sorted(({"name": s["name"], **{k: s[k] for k in jobs.TIMING}} for s in attached),
                               key=_canonical)
        out.append(CatalogObject(f"agent_job:{job['name']}", "agent_job", (
            Fact("skeleton", "job_id", job["job_id"], p),
            Fact("skeleton", "enabled", bool(job["enabled"]), p),
            Fact("skeleton", "description", job["description"], p),
            Fact("skeleton", "category", categories.get(job["category_id"]), p),
            Fact("skeleton", "start_step", job["start_step_id"], p),
            Fact("skeleton", "steps", steps, p),
            Fact("skeleton", "schedules", job_schedules, p),
            Fact("skeleton", "secrets", jobs.marked_steps(steps), Provenance("server", "msdb: dbo.sysjobsteps, marked "
                                                                                      "by the extractor's patterns")),
        )))
    return out
