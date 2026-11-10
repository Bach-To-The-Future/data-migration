"""Result shapes, described in the sandbox (spec section 3, issue #41).

SQL Server describes a procedure's first result set only to a login that holds EXECUTE, and
the extractor's login never does. So each procedure is recreated in the sandbox, a disposable
SQL Server the tool controls fully, with the client's settings (the database's compatibility
level and default collation, and the ANSI_NULLS and QUOTED_IDENTIFIER stored with the
procedure), and described there with sys.dm_exec_describe_first_result_set_for_object. A
database read from folders alone has views and inline functions with no columns either, since
those come from a server's metadata; they are created in the sandbox and their columns read
from sys.columns, as a live extraction reads them.

What the sandbox holds is recreated from the catalog alone: schemas, alias types, table types,
sequences, tables (columns and their types, collations and computed expressions; no keys,
defaults or indexes, which do not change a result's types), synonyms, views and functions
(created in passes until a pass creates none, since they may depend on one another), and
then the procedures. Anything it could not recreate is listed with SQL Server's reason, and a
procedure that depends on it says so through its own error.

A shape is one of three things in the catalog, and a procedure always has one of them:
`result_shape` (with an empty list for a procedure with no result set), or
`result_shape_unavailable` holding the reason ("could not be described"). Reading a
`result_shape` that is not there fails loudly through the query interface, never with an
empty shape.

The administrator's export (shape_export.py), read as a folder input, takes the place of the
sandbox for what it covers. Shapes carry provenance: "file" for the export, "assumed" when
described under a setting that was assumed (a database read from folders alone), and
"server" otherwise, the settings and definitions having been read from the server.
"""
from __future__ import annotations

import dataclasses
import re
import uuid
from typing import Callable

from ...core.catalog import CatalogObject, Fact, Provenance, Snapshot
from . import extractor, folders, names, shape_export
from .connection import Connection, SqlServerError

SOURCE = "sqlserver"
SANDBOX_IDENTITY = "sandbox_description"
SANDBOX_KIND = "sandbox_description"
EXPORT_KIND = folders.EXPORT_KIND
PROCEDURE_KINDS = ("procedure", "clr_procedure")
FILE_BUILT_KINDS = ("view", "inline_function")      # described from sys.columns in the sandbox
MODULE_KINDS = ("view", "scalar_function", "inline_function", "table_function", "procedure")
LOWEST_LEVEL = 100                                  # ADR 0015: no sandbox accepts a lower one
SANDBOX_DATABASE = "migrt_sandbox_"
DESCRIBED_BY = "sandbox: sys.dm_exec_describe_first_result_set_for_object"
READ_BY = "sandbox: sys.columns"
NO_SANDBOX = ("no sandbox was given, so it was not recreated and described there (spec section 3); give a sandbox, "
              "or an administrator's export of shapes")
# Error 7601: a CONTAINS or FREETEXT predicate on a table with no full-text index. Full-text
# search is manual work (spec section 6), and the sandbox holds no full-text indexes.
FULL_TEXT_ERROR = 7601
FULL_TEXT = ("full-text search, which is manual work: the procedure uses a CONTAINS or FREETEXT predicate, and the "
             "sandbox holds no full-text indexes (spec section 6)")
SAFE_COLLATION = re.compile(r"^[A-Za-z0-9_]+$")
SPACE = re.compile(r"\s+")
COMMENTS = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", re.S)


class SandboxUnavailable(Exception):
    """The sandbox cannot be reached, or could not be cleaned up. Nothing was stored."""


@dataclasses.dataclass(frozen=True)
class SandboxAddress:
    """The sandbox SQL Server and a login with full rights on it (it creates and drops its
    scratch databases). The tool's own disposable server, never the client's."""
    host: str
    port: int
    login: str
    password: str


class _Cannot(Exception):
    """An object the sandbox cannot be given, with the reason."""


# ---------------------------------------------------------------------------- DDL from the catalog

def quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _qualified(schema: str, name: str) -> str:
    return f"{quote(schema)}.{quote(name)}"


def _builtin(column: dict) -> str:
    """A system type with its length, precision and scale, as a column declares it."""
    system, length = column["system_type"], column["max_length"]
    if system in ("char", "varchar", "binary", "varbinary"):
        return f"{system}({'max' if length == -1 else length})"
    if system in ("nchar", "nvarchar"):
        return f"{system}({'max' if length == -1 else length // 2})"
    if system in ("decimal", "numeric"):
        return f"{system}({column['precision']}, {column['scale']})"
    if system in ("datetime2", "datetimeoffset", "time"):
        return f"{system}({column['scale']})"
    if system == "float":
        return f"float({column['precision']})"
    if system == "clr_type":
        raise _Cannot("a CLR user-defined type, which needs its assembly")
    return system


def _type_parts(identity: str, database: str, schemas: list[str]) -> tuple[str, str]:
    rest = identity.removeprefix(names.TYPE_PREFIX).removeprefix(database + ".")
    schema = max((s for s in schemas if rest.startswith(s + ".")), key=len, default=None)
    if schema is None:
        raise _Cannot(f"a user type {identity} in a schema the catalog does not hold")
    return schema, rest[len(schema) + 1:]


def _column_sql(column: dict, database: str, schemas: list[str]) -> str:
    if column.get("computed"):
        persisted = " PERSISTED" if column.get("computed_persisted") else ""
        return f"{quote(column['name'])} AS ({column['computed_expression']}){persisted}"
    collation = column.get("collation")
    if column.get("user_type"):
        schema, name = _type_parts(column["user_type"], database, schemas)
        declared, collation = _qualified(schema, name), None     # a column of an alias type takes its type's
    elif column["declared_type"] == "sysname":
        declared, collation = "sysname", None
    else:
        declared = _builtin(column)
    if collation and not SAFE_COLLATION.match(collation):
        raise _Cannot(f"a collation name SQL Server does not use: {collation!r}")
    collate = f" COLLATE {collation}" if collation else ""
    return f"{quote(column['name'])} {declared}{collate} {'NULL' if column['nullable'] else 'NOT NULL'}"


def _table_sql(obj: CatalogObject, database: str, schemas: list[str]) -> str:
    _, schema, name = obj.fact("name_parts").value
    columns = ", ".join(_column_sql(c, database, schemas) for c in obj.fact("columns").value)
    return f"CREATE TABLE {_qualified(schema, name)} ({columns})"


def _table_type_sql(obj: CatalogObject, database: str, schemas: list[str]) -> str:
    schema, name = _type_parts(obj.identity, database, schemas)
    columns = ", ".join(_column_sql(c, database, schemas) for c in obj.fact("columns").value)
    return f"CREATE TYPE {_qualified(schema, name)} AS TABLE ({columns})"


def _alias_type_sql(obj: CatalogObject, database: str, schemas: list[str]) -> str:
    if obj.has_fact("clr_type"):
        raise _Cannot("a CLR user-defined type, which needs its assembly")
    schema, name = _type_parts(obj.identity, database, schemas)
    base = obj.fact("base_type").value
    # CREATE TYPE takes no COLLATE clause: an alias type of a string type has the database's collation.
    return (f"CREATE TYPE {_qualified(schema, name)} FROM {_builtin({**base, 'declared_type': base['system_type']})} "
            f"{'NULL' if base['nullable'] else 'NOT NULL'}")


def _sequence_sql(obj: CatalogObject) -> str:
    _, schema, name = obj.fact("name_parts").value
    s = obj.fact("sequence").value
    if s["type"] in ("decimal", "numeric"):
        raise _Cannot("a sequence of a decimal type, whose precision the catalog does not hold")
    cache = "NO CACHE" if not s["cached"] else ("CACHE" if s["cache_size"] is None else f"CACHE {int(s['cache_size'])}")
    return (f"CREATE SEQUENCE {_qualified(schema, name)} AS {s['type']} START WITH {int(s['start'])} "
            f"INCREMENT BY {int(s['increment'])} MINVALUE {int(s['minimum'])} MAXVALUE {int(s['maximum'])} "
            f"{'CYCLE' if s['cycles'] else 'NO CYCLE'} {cache}")


def _synonym_sql(obj: CatalogObject) -> str:
    _, schema, name = obj.fact("name_parts").value
    return f"CREATE SYNONYM {_qualified(schema, name)} FOR " + ".".join(quote(p) for p in obj.fact("base_object").value)


def creatable(text: str) -> str:
    """A module's text as a CREATE statement. A module a folder defined and later altered
    holds its last statement, an ALTER, which creates nothing in an empty database."""
    leading = COMMENTS.match(text)
    start = leading.end() if leading else 0
    if text[start:start + 5].upper() == "ALTER" and not text[start + 5:start + 6].isalnum():
        return text[:start] + "CREATE OR ALTER" + text[start + 5:]
    return text


# ---------------------------------------------------------------------------- the sandbox

class _Scratch:
    """One scratch database in the sandbox, created for one client database and dropped
    after it, whatever happens."""

    def __init__(self, address: SandboxAddress, statements: list[str] | None):
        self.address, self.statements = address, statements
        self.name = SANDBOX_DATABASE + uuid.uuid4().hex[:12]

    def _open(self, database: str) -> Connection:
        a = self.address
        try:
            conn = Connection(a.host, a.port, a.login, a.password, database)
        except SqlServerError as failed:
            raise SandboxUnavailable(f"the sandbox {a.host},{a.port} cannot be reached as {a.login}: {failed}") from None
        if self.statements is not None:
            conn.statements = self.statements
        return conn

    def create(self, collation: str, level: int) -> None:
        if not SAFE_COLLATION.match(collation):
            raise SqlServerError([])
        with self._open("master") as master:
            master.query(f"CREATE DATABASE {quote(self.name)} COLLATE {collation}")
            master.query(f"ALTER DATABASE {quote(self.name)} SET COMPATIBILITY_LEVEL = {int(level)}")

    def connect(self) -> Connection:
        return self._open(self.name)

    def drop(self) -> None:
        with self._open("master") as master:
            try:
                master.query(f"IF DB_ID(N'{self.name}') IS NOT NULL BEGIN ALTER DATABASE {quote(self.name)} SET "
                             f"SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE {quote(self.name)}; END")
            except SqlServerError as failed:
                raise SandboxUnavailable(f"the sandbox's scratch database {self.name} could not be dropped; "
                                         f"drop it by hand: {failed}") from None


def check_reachable(address: SandboxAddress) -> None:
    """Fail loudly, before anything is stored, if the sandbox cannot be reached."""
    _Scratch(address, None)._open("master").close()


@dataclasses.dataclass
class _Recreation:
    """What a scratch database holds after the catalog was recreated in it."""
    connection: Connection
    database: str
    schemas: list[str]
    created: set[str] = dataclasses.field(default_factory=set)
    not_recreated: dict[str, str] = dataclasses.field(default_factory=dict)
    session: tuple[bool, bool] = (True, True)

    def run(self, sql: str, ansi_nulls: bool = True, quoted_identifier: bool = True, *,
            module: bool = False) -> None:
        wanted = (ansi_nulls, quoted_identifier)
        if module or wanted != self.session:
            self.connection.query(f"SET ANSI_NULLS {'ON' if ansi_nulls else 'OFF'}\n"
                                  f"SET QUOTED_IDENTIFIER {'ON' if quoted_identifier else 'OFF'}")
            self.session = wanted
        self.connection.query(sql)

    def attempt(self, identity: str, build: Callable[[], str], **settings) -> str | None:
        """Create one object; returns why it could not be created, or None. A failed batch
        leaves no object, so a later attempt may succeed once what it needs exists."""
        try:
            self.run(build(), **settings)
        except _Cannot as cannot:
            reason = str(cannot)
        except SqlServerError as failed:
            reason = FULL_TEXT if FULL_TEXT_ERROR in failed.numbers else str(failed)
        else:
            self.created.add(identity)
            self.not_recreated.pop(identity, None)
            return None
        self.not_recreated[identity] = reason
        return reason


def _module_settings(obj: CatalogObject) -> dict:
    return {"ansi_nulls": bool(obj.fact("ansi_nulls", SOURCE).value),
            "quoted_identifier": bool(obj.fact("quoted_identifier", SOURCE).value), "module": True}


def _recreatable(obj: CatalogObject) -> bool:
    return obj.has_fact("extractable") and bool(obj.fact("extractable").value)


def _recreate(conn: Connection, database: str, objects: list[CatalogObject]) -> _Recreation:
    schemas = sorted({o.fact("name_parts").value[1] for o in objects if o.has_fact("name_parts")}
                     | {_type_schema(o.identity, database, objects) for o in objects
                        if o.kind in ("user_type", "table_type")})
    r = _Recreation(conn, database, schemas)
    for schema in schemas:
        if schema.casefold() != "dbo":
            r.attempt(f"schema {schema}", lambda s=schema: f"CREATE SCHEMA {quote(s)}")
    for kind in ("user_type", "table_type"):
        for o in objects:
            if o.kind == kind:
                r.attempt(o.identity, lambda o=o: (_alias_type_sql if kind == "user_type" else _table_type_sql)(
                    o, database, schemas))
    for o in objects:
        if o.kind == "sequence":
            r.attempt(o.identity, lambda o=o: _sequence_sql(o))
    pending = [o for o in objects if o.kind in ("table", "synonym", "view", "scalar_function", "inline_function",
                                                  "table_function")]
    while pending:
        failed = []
        for o in pending:
            if o.kind in MODULE_KINDS:
                if not _recreatable(o):
                    r.not_recreated[o.identity] = _unextractable(o)
                    continue
                reason = r.attempt(o.identity, lambda o=o: creatable(o.fact("source_text").value),
                                   **_module_settings(o))
            elif o.kind == "table":
                reason = r.attempt(o.identity, lambda o=o: _table_sql(o, database, schemas))
            else:
                reason = r.attempt(o.identity, lambda o=o: _synonym_sql(o))
            if reason is not None:
                failed.append(o)
        if len(failed) == len(pending):
            break
        pending = failed
    for o in objects:
        if o.kind == "procedure" and _recreatable(o):
            r.attempt(o.identity, lambda o=o: creatable(o.fact("source_text").value), **_module_settings(o))
    return r


def _type_schema(identity: str, database: str, objects: list[CatalogObject]) -> str:
    """A user type's schema: the longest schema of the database's objects that the identity names."""
    rest = identity.removeprefix(names.TYPE_PREFIX).removeprefix(database + ".")
    known = {o.fact("name_parts").value[1] for o in objects if o.has_fact("name_parts")}
    return max((s for s in known if rest.startswith(s + ".")), key=len, default=rest.split(".", 1)[0])


def _unextractable(obj: CatalogObject) -> str:
    reason = obj.fact("not_extractable_reason").value if obj.has_fact("not_extractable_reason") else "no source text"
    return f"{reason}; it cannot be recreated in the sandbox"


# ---------------------------------------------------------------------------- describing

DESCRIBE = """SELECT column_ordinal, name, is_nullable, system_type_name, max_length, precision, scale,
       user_type_name, error_number, error_message
FROM sys.dm_exec_describe_first_result_set_for_object(OBJECT_ID(@name), 0)
ORDER BY column_ordinal"""


def described_shape(rows: list[dict]) -> list[dict] | str:
    """The shape a description gives, or the reason it could not be described."""
    if rows and rows[0]["error_number"] is not None:
        return f"SQL Server could not describe it (error {rows[0]['error_number']}): {rows[0]['error_message'].strip()}"
    out = []
    for r in rows:
        base = shape_export.base_type(r["system_type_name"])
        if base is None:
            raise ValueError(f"SQL Server described a column of type {r['system_type_name']!r}")
        out.append({"name": r["name"], "declared_type": r["user_type_name"] or base, "system_type": base,
                    "max_length": int(r["max_length"]), "precision": int(r["precision"]), "scale": int(r["scale"]),
                    "nullable": bool(r["is_nullable"])})
    return out


def _view_shapes(conn: Connection) -> dict[tuple[str, str], list[dict]]:
    """The columns of every view and function in the scratch database, read as the extractor
    reads a live server's: from sys.columns and sys.types."""
    cap = {v: extractor.capture(conn, v) for v in ("sys.objects", "sys.schemas", "sys.columns", "sys.types")}
    schemas = {r["schema_id"]: r["name"] for r in cap["sys.schemas"].rows}
    types = {r["user_type_id"]: r for r in cap["sys.types"].rows}
    columns: dict[int, list[dict]] = {}
    for c in sorted(cap["sys.columns"].rows, key=lambda r: (r["object_id"], r["column_id"])):
        columns.setdefault(c["object_id"], []).append(
            {"name": c["name"], **extractor.type_shape_of(c, types), "nullable": bool(c["is_nullable"])})
    return {(schemas[o["schema_id"]], o["name"]): columns.get(o["object_id"], []) for o in cap["sys.objects"].rows}


def _database_of(obj: CatalogObject, databases: list[str]) -> str | None:
    if obj.has_fact("name_parts"):
        return obj.fact("name_parts").value[0]
    stripped = obj.identity.removeprefix(names.TYPE_PREFIX)
    return max((d for d in databases if stripped.startswith(d + ".")), key=len, default=None)


@dataclasses.dataclass
class _Outcome:
    shape: list[dict] | None = None
    reason: str | None = None
    sandbox: bool = False


def _describe_database(address: SandboxAddress, statements: list[str] | None, database: CatalogObject,
                       objects: list[CatalogObject], targets: list[CatalogObject]) -> tuple[dict[str, _Outcome], dict]:
    """Recreate one database's catalog in a scratch database and describe the targets there.
    Returns each target's outcome and what the sandbox did, for the report."""
    level = int(database.fact("compatibility_level", SOURCE).value)
    collation = database.fact("collation").value
    done = {"compatibility_level": level, "collation": collation, "recreated": 0, "not_recreated": []}

    def refuse(reason: str) -> tuple[dict[str, _Outcome], dict]:
        return {t.identity: _Outcome(reason=reason) for t in targets}, done

    if level < LOWEST_LEVEL:
        return refuse(f"compatibility level {level} is below {LOWEST_LEVEL}, which no sandbox accepts (ADR 0015): "
                      "its shapes come from the guarded remote tier if one exists, else the administrator's export "
                      "(spec section 3)")
    scratch = _Scratch(address, statements)
    try:
        try:
            scratch.create(collation, level)
        except SqlServerError as failed:
            return refuse(f"the sandbox could not create a database with collation {collation} at compatibility "
                          f"level {level}, so it does not accept the client's settings: {failed}")
        with scratch.connect() as conn:
            made = _recreate(conn, database.identity, objects)
            outcomes: dict[str, _Outcome] = {}
            views = _view_shapes(conn) if any(t.kind in FILE_BUILT_KINDS for t in targets) else {}
            for t in targets:
                outcomes[t.identity] = _describe(conn, made, t, views)
        done["recreated"] = len(made.created) - sum(1 for i in made.created if i.startswith("schema "))
        done["not_recreated"] = sorted(({"object": i, "reason": why} for i, why in made.not_recreated.items()),
                                       key=lambda e: e["object"])
        return outcomes, done
    finally:
        scratch.drop()


def _describe(conn: Connection, made: _Recreation, target: CatalogObject, views: dict) -> _Outcome:
    _, schema, name = target.fact("name_parts").value
    if target.kind in FILE_BUILT_KINDS:
        if target.identity not in made.created:
            return _Outcome(reason=f"it could not be recreated in the sandbox: {made.not_recreated[target.identity]}")
        return _Outcome(shape=views[(schema, name)], sandbox=True)
    if not _recreatable(target):
        return _Outcome(reason=_unextractable(target))
    if target.identity not in made.created:
        return _Outcome(reason=f"it could not be recreated in the sandbox: {made.not_recreated[target.identity]}")
    described = described_shape(conn.query(DESCRIBE, {"name": _qualified(schema, name)}))
    if isinstance(described, str):
        return _Outcome(reason=described)
    return _Outcome(shape=described, sandbox=True)


# ---------------------------------------------------------------------------- attaching to a snapshot

def _is_target(obj: CatalogObject) -> bool:
    return obj.kind in PROCEDURE_KINDS or (obj.kind in FILE_BUILT_KINDS and obj.has_fact("result_shape_unavailable"))


def _exports(snapshot: Snapshot) -> dict[str, tuple[str, shape_export.Entry]]:
    """Each procedure the administrator's exports describe, with the file that does."""
    found: dict[str, tuple[str, shape_export.Entry]] = {}
    for o in snapshot.objects:
        if o.kind != EXPORT_KIND:
            continue
        label = o.fact("file").value
        for e in o.fact("entries").value:
            if e["object"] in found:
                raise ExportsDisagree(e["object"], found[e["object"]][0], label)
            schema, procedure = e["object"].rsplit(".", 2)[1:]
            found[e["object"]] = (label, shape_export.Entry(schema, procedure, e["shape"], e["error"]))
    return found


class ExportsDisagree(folders.FolderExtractionError):
    def __init__(self, identity: str, first: str, second: str):
        super().__init__(f"two result-shape exports both describe {identity}: {first} and {second}. Keep one.")


def _settings_provenance(snapshot: Snapshot, target: CatalogObject, database: CatalogObject) -> tuple[str, list[str]]:
    """Where the settings a shape was described under came from: server, unless any was assumed."""
    used = [("the compatibility level", database.fact("compatibility_level", SOURCE).provenance),
            ("the default collation", database.fact("collation").provenance)]
    for setting in ("ansi_nulls", "quoted_identifier"):
        if target.has_fact(setting, SOURCE):
            used.append((f"the module's {setting.upper()}", target.fact(setting, SOURCE).provenance))
    assumed = [what for what, p in used if p.kind == "assumed"]
    own = target.fact("source_text").provenance.kind if target.has_fact("source_text") else "server"
    return ("assumed" if assumed else ("file" if own == "file" else "server")), assumed


def _unavailable_kind(target: CatalogObject) -> str:
    return "file" if target.has_fact("source_text") and target.fact("source_text").provenance.kind == "file" else "server"


def attach(snapshot: Snapshot, sandbox: SandboxAddress | None, *, statements: list[str] | None = None) -> Snapshot:
    """The snapshot with every procedure's result shape (or why it could not be described),
    and the columns of views and inline functions read from folders. The administrator's
    export covers what it holds; the sandbox the rest; with neither, the reason says so.
    `statements` collects every batch sent to the sandbox. Raises SandboxUnavailable when
    the sandbox is given and cannot be reached, before anything is described."""
    if sandbox is not None:
        check_reachable(sandbox)
    exports = _exports(snapshot)
    targets = [o for o in snapshot.objects if _is_target(o)]
    databases = [o.identity for o in snapshot.objects if o.kind == "database"]
    by_database: dict[str, list[CatalogObject]] = {}
    outcomes: dict[str, tuple[_Outcome, Provenance]] = {}
    for t in targets:
        db = _database_of(t, databases)
        if db is None:
            raise KeyError(f"{t.identity} is in no database of the snapshot")
        if t.identity in exports:
            label, entry = exports[t.identity]
            reason = None if entry.error is None else f"{entry.error} (the administrator's export {label})"
            outcomes[t.identity] = (_Outcome(shape=entry.shape, reason=reason), Provenance("file", label))
        elif t.kind == "clr_procedure" or not _recreatable(t):
            outcomes[t.identity] = (_Outcome(reason=_unextractable(t)),
                                    Provenance(_unavailable_kind(t), "not described: it cannot be recreated"))
        elif sandbox is None:
            outcomes[t.identity] = (_Outcome(reason=NO_SANDBOX), Provenance(_unavailable_kind(t),
                                                                              "not described: no sandbox was given"))
        else:
            by_database.setdefault(db, []).append(t)
    ran: dict[str, dict] = {}
    for db in sorted(by_database):
        assert sandbox is not None   # only a given sandbox leaves targets for it
        database = snapshot.get(db)
        objects = [o for o in snapshot.objects if _database_of(o, databases) == db and o.kind != "database"]
        results, done = _describe_database(sandbox, statements, database, objects, by_database[db])
        ran[db] = done
        for t in by_database[db]:
            kind, assumed = _settings_provenance(snapshot, t, database)
            outcome = results[t.identity]
            how = (READ_BY if t.kind in FILE_BUILT_KINDS else DESCRIBED_BY) if outcome.sandbox else "not described"
            detail = how + (f"; assumed: {', '.join(assumed)}" if assumed else "")
            outcomes[t.identity] = (outcome, Provenance(kind, detail))
    unmatched = sorted(({"file": label, "object": identity} for identity, (label, _) in exports.items()
                        if not snapshot.has(identity)), key=lambda e: (e["file"], e["object"]))
    added: dict[str, tuple[Fact, ...]] = {}
    for identity, (outcome, provenance) in outcomes.items():
        fact = (Fact("skeleton", "result_shape", outcome.shape, provenance) if outcome.reason is None else
                Fact("skeleton", "result_shape_unavailable", outcome.reason, provenance))
        added[identity] = (fact,)
    objects = []
    for o in snapshot.objects:
        if o.identity == SANDBOX_IDENTITY:
            continue        # an earlier attach's record; this one replaces it
        if o.identity in added:
            kept = tuple(f for f in o.facts if f.name not in ("result_shape", "result_shape_unavailable"))
            o = dataclasses.replace(o, facts=kept + added[o.identity])
        objects.append(o)
    recorded = []
    if sandbox is not None:
        recorded.append(Fact("skeleton", "databases", ran, Provenance("server", "the sandbox's recreation of the catalog")))
    if exports:
        recorded.append(Fact("skeleton", "export_unmatched", unmatched,
                             Provenance("file", ", ".join(sorted({label for label, _ in exports.values()})))))
    if recorded:
        objects.append(CatalogObject(SANDBOX_IDENTITY, SANDBOX_KIND, tuple(recorded)))
    return Snapshot.of(snapshot.source, objects)


# ---------------------------------------------------------------------------- findings

def findings(snapshot: Snapshot) -> dict:
    """What the report shows of result shapes: how many procedures were described, by the
    sandbox or by an export, every object that could not be described with its reason, every
    object the sandbox could not recreate, and every export entry for an object the catalog lacks."""
    exports = {o.fact("file").value for o in snapshot.objects if o.kind == EXPORT_KIND}
    procedures = [o for o in snapshot.objects if o.kind in PROCEDURE_KINDS]
    shaped = [o for o in snapshot.objects if o.kind in PROCEDURE_KINDS + FILE_BUILT_KINDS and o.has_fact("result_shape")]
    sandbox = sum(1 for o in shaped if o.fact("result_shape").provenance.detail.startswith("sandbox:"))
    from_export = sum(1 for o in shaped if o.fact("result_shape").provenance.detail in exports)
    could_not = sorted(({"object": o.identity, "reason": o.fact("result_shape_unavailable").value,
                         "procedure": o.kind in PROCEDURE_KINDS}
                        for o in snapshot.objects if o.kind in PROCEDURE_KINDS + FILE_BUILT_KINDS
                        and o.has_fact("result_shape_unavailable")), key=lambda e: e["object"])
    description = snapshot.get(SANDBOX_IDENTITY) if snapshot.has(SANDBOX_IDENTITY) else None
    ran = description.fact("databases").value if description and description.has_fact("databases") else {}
    return {
        "procedures": {"total": len(procedures), "described": sum(1 for o in procedures if o.has_fact("result_shape"))},
        "described_in_sandbox": sandbox,
        "described_from_export": from_export,
        "could_not_be_described": could_not,
        "not_recreated": sorted(({"object": e["object"], "reason": e["reason"], "database": db}
                                 for db, d in ran.items() for e in d["not_recreated"]),
                                key=lambda e: (e["database"], e["object"])),
        "export_unmatched": (description.fact("export_unmatched").value
                             if description and description.has_fact("export_unmatched") else []),
    }
