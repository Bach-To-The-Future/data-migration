"""Extraction from folders of exported files (spec section 3, "Inputs and provenance" and
"Extraction from folders alone").

An extraction takes at most one live server and any number of folders, each named for the
database its files belong to. Every file is kept verbatim in the raw capture, and read by
its role:

- **Data definition**: a .sql file holding only data-definition statements (with SET, USE
  and PRINT). For a database read from no server, its statements are replayed in a fixed
  order to build the catalog: every CREATE before every ALTER, file-name order within each,
  statement order within a file; CREATE INDEX and CREATE OR ALTER count as CREATE. A
  second CREATE of the same object is an extraction error naming both files. Beside a live server, the server
  is the source of its databases' objects, and such files are not replayed; each statement
  is listed as superseded (ADR 0025).
- **Query batch**: any other .sql file. It is an artefact, kept whole; its own data
  definition is code it runs, not a table shape.
- **SQL Agent job script**: a .sql file calling `sp_add_job`. Read only without a live server,
  whose msdb is otherwise the source of jobs.
- **SSIS package**: a .dtsx file, inventoried (ssis.py).
- Any other file is not read, and is listed.

Every fact records its provenance: parsed from a named file and line, or assumed. What a
server would have supplied takes SQL Server's documented default and is recorded as assumed:
in the `assumed_values` fact of the object it belongs to when it is part of a parsed fact
(a column's collation or nullability, a constraint's name), or as an assumed fact. The
database default collation is a required setting for every database read from folders
alone, and the time zone for any extraction without a server.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re
import xml.etree.ElementTree as ET
from typing import Any, Callable

from ...core.catalog import CatalogObject, Fact, Provenance, Snapshot
from . import jobs, names, parser, resolution, secrets, ssis

SOURCE = "sqlserver"
SQL_SUFFIXES = (".sql",)
PACKAGE_SUFFIXES = (".dtsx",)
FOLDERS_IDENTITY = "folders"
NO_SERVER = "(no server)"

# The roles a file can have.
DATA_DEFINITION, QUERY_BATCH, JOB_SCRIPT, PACKAGE = "data definition", "query batch", "SQL Agent job script", "SSIS package"
NOT_READ, DID_NOT_PARSE = "not read", "did not parse"

# What a server supplies and a folder does not (spec section 3), for the report.
LOST_WITHOUT_A_SERVER = ("server and database settings", "per-routine settings unless the script sets them",
                         "row counts", "trigger firing order", "confirmation that an object exists")
# The assumptions, each SQL Server's documented default.
CLIENT_DEFAULT_ON = "not set in the file: ON, the default of SQL Server's client drivers and of SSMS"
NULL_DEFAULT = ("no NULL or NOT NULL: nullable, as ANSI_NULL_DFLT_ON, the default of SQL Server's client drivers, "
                "makes it")
COMPUTED_NULLABLE = "a computed column's nullability is worked out by SQL Server from its expression"
COLLATION_DEFAULT = "no COLLATE clause: the database default collation, a required setting"
NAMED_BY_SERVER = "not named in the file: SQL Server names it when it is created"
DEFAULT_SCHEMA = "a one-part name is created in the creator's default schema, assumed dbo"
SEQUENCE_DEFAULT = "not in the CREATE SEQUENCE statement: SQL Server's default"
FIRING_ORDER = "not set in the files: a trigger fires in no set order until sp_settriggerorder sets one"
VERSION_DEFAULT = "16"   # SQL Server 2022, the release the sandbox runs
RELEASES = {"14": "2017", "15": "2019", "16": "2022", "17": "2025"}
LEVELS = {"14": 140, "15": 150, "16": 160, "17": 170}

# Built-in types: (max_length, precision, scale) as sys.types and sys.columns report them.
FIXED_TYPES = {"bigint": (8, 19, 0), "int": (4, 10, 0), "smallint": (2, 5, 0), "tinyint": (1, 3, 0), "bit": (1, 1, 0),
               "money": (8, 19, 4), "smallmoney": (4, 10, 4), "real": (4, 24, 0), "float": (8, 53, 0),
               "date": (3, 10, 0), "datetime": (8, 23, 3), "smalldatetime": (4, 16, 0),
               "uniqueidentifier": (16, 0, 0), "text": (16, 0, 0), "ntext": (16, 0, 0), "image": (16, 0, 0),
               "xml": (-1, 0, 0), "sql_variant": (8016, 0, 0), "timestamp": (8, 0, 0)}
CLR_SYSTEM_TYPES = {"hierarchyid": (892, 0, 0), "geometry": (-1, 0, 0), "geography": (-1, 0, 0)}
ALIAS_TYPES = {"sysname": ("nvarchar", 256, 0, 0, False)}   # system type, length, precision, scale, nullable
COLLATABLE = {"char", "varchar", "nchar", "nvarchar", "text", "ntext"}
SYSTEM_TYPE_NAMES = frozenset({*FIXED_TYPES, *CLR_SYSTEM_TYPES, *ALIAS_TYPES, "char", "varchar", "nchar", "nvarchar",
                               "binary", "varbinary", "decimal", "numeric", "datetime2", "time", "datetimeoffset",
                               "rowversion", "cursor", "table"})

CREATES = {"CreateTableStatement", "CreateViewStatement", "CreateProcedureStatement", "CreateFunctionStatement",
           "CreateTriggerStatement", "CreateSynonymStatement", "CreateSequenceStatement", "CreateTypeUddtStatement",
           "CreateTypeTableStatement", "CreateTypeUdtStatement", "CreateSchemaStatement"}
INDEXES = {"CreateIndexStatement"}
# A CREATE OR ALTER is a CREATE: a second one of the same object is a duplicate.
CREATE_OR_ALTER = {"CreateOrAlterViewStatement", "CreateOrAlterProcedureStatement", "CreateOrAlterFunctionStatement",
                   "CreateOrAlterTriggerStatement"}
ALTERS = {"AlterTableAddTableElementStatement", "AlterTableAlterColumnStatement", "AlterTableDropTableElementStatement",
          "AlterTableConstraintModificationStatement", "AlterTableTriggerModificationStatement", "AlterViewStatement",
          "AlterProcedureStatement", "AlterFunctionStatement", "AlterTriggerStatement", "EnableDisableTriggerStatement"}
SESSION = {"UseStatement", "PrintStatement"}
NOT_UNIQUE = "an index that is not unique, which the catalog does not hold"
# Data definition that builds no catalog fact, each listed with the reason when it is met.
NOT_REPLAYED = {
    "GrantStatement": "security metadata, which the catalog does not hold",
    "DenyStatement": "security metadata, which the catalog does not hold",
    "RevokeStatement": "security metadata, which the catalog does not hold",
    "CreateUserStatement": "security metadata, which the catalog does not hold",
    "CreateRoleStatement": "security metadata, which the catalog does not hold",
    "AlterRoleStatement": "security metadata, which the catalog does not hold",
    "CreateLoginStatement": "security metadata, which the catalog does not hold",
    "CreateStatisticsStatement": "statistics, which the catalog does not hold",
    "UpdateStatisticsStatement": "statistics, which the catalog does not hold",
    "CreateXmlIndexStatement": NOT_UNIQUE,
    "CreateColumnStoreIndexStatement": NOT_UNIQUE,
    "CreateSpatialIndexStatement": NOT_UNIQUE,
    "CreateFullTextIndexStatement": "a full-text index, which the catalog does not hold",
    "CreateFullTextCatalogStatement": "a full-text catalog, which the catalog does not hold",
    "AlterIndexStatement": "index maintenance, which changes no table shape",
    "AlterSequenceStatement": "a change to a sequence, which the replay does not apply",
    "AlterSchemaStatement": "a transfer between schemas, which the replay does not apply",
    "AlterTableSetStatement": "table options, which change no table shape",
    "AlterTableSwitchStatement": "partition switching, which changes no table shape",
    "AlterTableRebuildStatement": "a rebuild, which changes no table shape",
    "AlterTableChangeTrackingModificationStatement": "change tracking, which changes no table shape",
    "CreatePartitionFunctionStatement": "partitioning, which changes no table shape",
    "CreatePartitionSchemeStatement": "partitioning, which changes no table shape",
    "CreateXmlSchemaCollectionStatement": "an XML schema collection, which the catalog does not hold",
    "CreateAssemblyStatement": "a CLR assembly, which the catalog does not hold",
}
DROP_REASON = "a DROP: a folder's data definition is replayed as the objects stand, so drops are not applied"
EXTENDED_PROPERTY = ("sp_addextendedproperty", "sp_updateextendedproperty", "sp_dropextendedproperty")
TRIGGER_ORDER = "sp_settriggerorder"
COMMENTS = {"SingleLineComment", "MultilineComment"}


class FolderExtractionError(Exception):
    """A folder cannot be extracted as given. Nothing was stored."""


class DuplicateCreate(FolderExtractionError):
    def __init__(self, identity: str, first: str, second: str):
        self.identity, self.first, self.second = identity, first, second
        super().__init__(f"{identity} is created twice, in {first} and in {second}. Remove one, then extract again.")


@dataclasses.dataclass(frozen=True)
class FolderInput:
    """A folder of exported files, and the database they belong to."""
    path: pathlib.Path
    database: str


@dataclasses.dataclass(frozen=True)
class SourceFile:
    folder: FolderInput
    relative: str            # posix, relative to the folder
    text: str | None         # None for a file that is not read
    encoding: str | None

    @property
    def label(self) -> str:
        return f"{self.folder.database}/{self.relative}"

    @property
    def suffix(self) -> str:
        return pathlib.PurePosixPath(self.relative).suffix.casefold()


@dataclasses.dataclass(frozen=True)
class DatabaseContext:
    """What a database's text is parsed and its names resolved with, whatever its input."""
    name: str
    collation: str
    compatibility_level: int
    live: bool
    system_types: frozenset[str] = SYSTEM_TYPE_NAMES
    schemas: frozenset[str] = frozenset({"dbo"})

    @property
    def case_sensitive(self) -> bool:
        return names.identifiers_case_sensitive(self.collation)

    def step(self) -> jobs.StepDatabase:
        return jobs.StepDatabase(self.compatibility_level, self.case_sensitive, self.system_types,
                                 frozenset(s.casefold() for s in self.schemas))


def _decode(data: bytes, where: str) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), "utf-8 with BOM"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as failed:
        raise FolderExtractionError(f"{where} is neither UTF-8 nor UTF-16 ({failed}). Save it as UTF-8, then extract "
                                    "again.") from None


def read_files(folders: list[FolderInput]) -> list[SourceFile]:
    """Every file in every folder, in file-name order: by its path in its folder, then by
    the folder's database. .sql and .dtsx files are read; the others are only listed."""
    files: list[SourceFile] = []
    for folder in folders:
        root = pathlib.Path(folder.path)
        if not root.is_dir():
            raise FolderExtractionError(f"{root} is not a folder")
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            relative = path.relative_to(root).as_posix()
            if path.suffix.casefold() in SQL_SUFFIXES + PACKAGE_SUFFIXES:
                text, encoding = _decode(path.read_bytes(), f"{folder.database}/{relative}")
                files.append(SourceFile(folder, relative, text, encoding))
            else:
                files.append(SourceFile(folder, relative, None, None))
    seen: dict[str, SourceFile] = {}
    for f in files:
        if f.label in seen:
            raise FolderExtractionError(f"two folders for database {f.folder.database} both hold {f.relative}: "
                                        f"{seen[f.label].folder.path} and {f.folder.path}")
        seen[f.label] = f
    return sorted(files, key=lambda f: (f.relative, f.folder.database))


# ---------------------------------------------------------------------------- ScriptDom helpers

def _kind(fragment) -> str:
    return str(fragment.GetType().Name)


def _text(text: str, fragment) -> str:
    return text[int(fragment.StartOffset):int(fragment.StartOffset) + int(fragment.FragmentLength)]


def _int(text: str, fragment) -> int:
    return int(re.sub(r"[\s()]", "", _text(text, fragment)))


def _id(identifier) -> str | None:
    return None if identifier is None else str(identifier.Value)


def _parts(son) -> list[str | None]:
    """server, database, schema, name, as written (None where left out)."""
    return [_id(son.ServerIdentifier), _id(son.DatabaseIdentifier), _id(son.SchemaIdentifier), _id(son.BaseIdentifier)]


def _flags(value) -> set[str]:
    return {s.strip() for s in str(value).split(",")}


def _snake(name: str) -> str:
    """ScriptDom's CreateTable as SQL Server's CREATE_TABLE."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).upper()


def _statements(statements) -> list:
    """Top-level statements, with IF branches and BEGIN ... END blocks opened."""
    out = []
    for s in statements:
        kind = _kind(s)
        if kind == "IfStatement":
            out += _statements([b for b in (s.ThenStatement, s.ElseStatement) if b is not None])
        elif kind == "BeginEndBlockStatement":
            out += _statements(list(s.StatementList.Statements))
        else:
            out.append(s)
    return out


def _procedure(statement) -> str | None:
    if _kind(statement) != "ExecuteStatement":
        return None
    entity = statement.ExecuteSpecification.ExecutableEntity
    if _kind(entity) != "ExecutableProcedureReference" or entity.ProcedureReference is None:
        return None
    return str(entity.ProcedureReference.ProcedureReference.Name.BaseIdentifier.Value).casefold()


def _is_data_definition(statement) -> bool:
    kind = _kind(statement)
    return (kind in CREATES or kind in INDEXES or kind in CREATE_OR_ALTER or kind in ALTERS or kind in SESSION
            or kind in NOT_REPLAYED
            or kind.startswith("Drop") or (kind.startswith("Set") and kind != "SetVariableStatement")
            or _procedure(statement) in (TRIGGER_ORDER, *EXTENDED_PROPERTY))


def _calls_job_procedures(parsed: parser.Parsed) -> bool:
    found = []

    def visit(f) -> None:
        if _kind(f) == "ExecuteSpecification":
            entity = f.ExecutableEntity
            if _kind(entity) == "ExecutableProcedureReference" and entity.ProcedureReference is not None:
                name = str(entity.ProcedureReference.ProcedureReference.Name.BaseIdentifier.Value).casefold()
                if name == "sp_add_job":
                    found.append(name)
    parser.walk(parsed.tree, visit)
    return bool(found)


def role_of(parsed: parser.Parsed) -> str:
    if _calls_job_procedures(parsed):
        return JOB_SCRIPT
    statements = _statements([s for b in parsed.tree.Batches for s in b.Statements])
    return DATA_DEFINITION if all(_is_data_definition(s) for s in statements) else QUERY_BATCH


# ---------------------------------------------------------------------------- statements in order

@dataclasses.dataclass
class _Setting:
    value: bool
    where: str | None       # the file and line that set it, or None where assumed


@dataclasses.dataclass
class _Statement:
    file: SourceFile
    parsed: parser.Parsed
    fragment: Any
    kind: str
    line: int
    database: str            # the database it runs in: the folder's, or one a USE named
    ansi_nulls: _Setting
    quoted_identifier: _Setting
    null_default: _Setting | None

    @property
    def where(self) -> str:
        return f"{self.file.label} line {self.line}"

    @property
    def text(self) -> str:
        return self.parsed.text

    def source(self) -> str:
        """The statement's text with the comments right before and after it, as a module's
        definition holds them."""
        tokens = self.parsed.tree.ScriptTokenStream
        first, last = int(self.fragment.FirstTokenIndex), int(self.fragment.LastTokenIndex)
        start = int(tokens[first].Offset)
        i = first - 1
        while i >= 0 and str(tokens[i].TokenType) in COMMENTS | {"WhiteSpace"}:
            if str(tokens[i].TokenType) in COMMENTS:
                start = int(tokens[i].Offset)
            i -= 1
        end = int(tokens[last].Offset) + len(str(tokens[last].Text))
        i = last + 1
        while i < tokens.Count and str(tokens[i].TokenType) in COMMENTS | {"WhiteSpace"}:
            if str(tokens[i].TokenType) in COMMENTS:
                end = int(tokens[i].Offset) + len(str(tokens[i].Text))
            i += 1
        return self.text[start:end]


def statements_of(file: SourceFile, parsed: parser.Parsed, databases: dict[str, DatabaseContext]) -> list[_Statement]:
    """A data-definition file's statements in order, each with the settings in force at it."""
    ansi = _Setting(True, None)
    quoted = _Setting(True, None)
    null_default: _Setting | None = None
    database = file.folder.database
    out = []
    for statement in _statements([s for b in parsed.tree.Batches for s in b.Statements]):
        kind, line = _kind(statement), int(statement.StartLine)
        here = f"{file.label} line {line}"
        if kind == "PredicateSetStatement":
            flags, on = _flags(statement.Options), bool(statement.IsOn)
            if "AnsiNulls" in flags:
                ansi = _Setting(on, here)
            if "QuotedIdentifier" in flags:
                quoted = _Setting(on, here)
            if "AnsiNullDfltOn" in flags:
                null_default = _Setting(on, here)
            if "AnsiNullDfltOff" in flags:
                null_default = _Setting(not on, here)
            continue
        if kind == "UseStatement":
            database = _canonical_database(str(statement.DatabaseName.Value), databases) or str(
                statement.DatabaseName.Value)
            continue
        out.append(_Statement(file, parsed, statement, kind, line, database, ansi, quoted, null_default))
    return out


def _canonical_database(name: str, databases: dict[str, DatabaseContext]) -> str | None:
    return next((d for d in databases if d.casefold() == name.casefold()), None)


# ---------------------------------------------------------------------------- types

def type_shape(data_type, text: str, user_types: Callable[[list[str | None]], dict | None]) -> tuple[dict, list[dict]]:
    """A column's or parameter's declared type as the catalog holds it (as extractor's
    _type_shape does from sys.types), with "collatable", the type's own nullability where
    it has one, and its user type's identity. Returns it and anything assumed about it."""
    kind = _kind(data_type)
    shape: dict = {"user_type": None, "collatable": False, "type_nullable": None}
    if kind == "XmlDataTypeReference":
        return {**shape, "declared_type": "xml", "system_type": "xml", "max_length": -1, "precision": 0, "scale": 0}, []
    if kind == "SqlDataTypeReference":
        name = str(data_type.SqlDataTypeOption).casefold()
        name = {"rowversion": "timestamp"}.get(name, name)
        params = [(_text(text, p) if _kind(p) != "MaxLiteral" else "max") for p in data_type.Parameters]
        return {**shape, **_builtin(name, params)}, []
    parts = _parts(data_type.Name)
    base = (parts[3] or "").casefold()
    if parts[2] is None and base in ALIAS_TYPES:
        system, length, precision, scale, nullable = ALIAS_TYPES[base]
        return {**shape, "declared_type": base, "system_type": system, "max_length": length, "precision": precision,
                "scale": scale, "collatable": True, "type_nullable": nullable}, []
    if parts[2] is None and base in CLR_SYSTEM_TYPES:
        length, precision, scale = CLR_SYSTEM_TYPES[base]
        return {**shape, "declared_type": base, "system_type": base, "max_length": length, "precision": precision,
                "scale": scale}, []
    found = user_types(parts)
    if found is None:
        written = ".".join(p for p in parts if p)
        return {**shape, "declared_type": parts[3], "system_type": None, "max_length": None, "precision": None,
                "scale": None}, [{"property": "system_type", "value": None,
                                  "reason": f"the type {written} is not created in the folders"}]
    return {**shape, "declared_type": parts[3], **found}, []


def _builtin(name: str, params: list[str]) -> dict:
    def n(i: int, default: int) -> int:
        return int(params[i]) if len(params) > i else default

    def out(declared: str, length: int, precision: int, scale: int) -> dict:
        return {"declared_type": declared, "system_type": declared, "max_length": length, "precision": precision,
                "scale": scale, "collatable": declared in COLLATABLE}

    if name in FIXED_TYPES:
        return out(name, *FIXED_TYPES[name])
    if name == "float":
        bits = n(0, 53)
        return out("real", 4, 24, 0) if bits <= 24 else out("float", 8, 53, 0)
    if name in ("char", "varchar", "binary", "varbinary", "nchar", "nvarchar"):
        size = 2 if name.startswith("n") else 1
        length = -1 if params[:1] == ["max"] else n(0, 1) * size
        return out(name, length, 0, 0)
    if name in ("decimal", "numeric"):
        p, s = n(0, 18), n(1, 0)
        return out(name, 5 if p <= 9 else 9 if p <= 19 else 13 if p <= 28 else 17, p, s)
    if name in ("datetime2", "time", "datetimeoffset"):
        s = n(0, 7)
        size = 0 if s <= 2 else 1 if s <= 4 else 2
        base = {"datetime2": (6, 19), "time": (3, 8), "datetimeoffset": (8, 26)}[name]
        return out(name, base[0] + size, base[1] + (s + 1 if s > 0 else 0), s)
    raise FolderExtractionError(f"the built-in type {name} is not known to the folder extractor")


# ---------------------------------------------------------------------------- objects being replayed

@dataclasses.dataclass
class _Object:
    identity: str
    kind: str
    database: str
    schema: str | None
    name: str
    where: list[str]
    assumed: list[dict] = dataclasses.field(default_factory=list)
    data: dict = dataclasses.field(default_factory=dict)

    def touched(self, statement: _Statement) -> None:
        if statement.where not in self.where:
            self.where.append(statement.where)

    @property
    def provenance(self) -> Provenance:
        return Provenance("file", "; ".join(self.where))


class _Replay:
    """The data definition of every database read from folders alone, replayed."""

    def __init__(self, databases: dict[str, DatabaseContext]):
        self.databases = databases
        self.objects: dict[str, _Object] = {}      # by folded identity
        self.schemas: dict[str, set[str]] = {d: {"dbo"} for d in databases}
        self.not_replayed: list[dict] = []

    # ------------------------------------------------------------ names

    def key(self, identity: str, database: str) -> str:
        return identity if self.databases[database].case_sensitive else identity.casefold()

    def target(self, son, statement: _Statement) -> tuple[str, str, str, bool] | None:
        """database, schema, name and whether the schema was assumed; None when the name is
        in a database this extraction does not replay, recorded."""
        server, database, schema, name = _parts(son)
        where = _canonical_database(database or statement.database, self.databases)
        if server or where is None:
            self.skip(statement, f"names {'a linked server' if server else 'a database this extraction does not read'}"
                                 f" ({'.'.join(p for p in _parts(son) if p)})")
            return None
        return where, schema or "dbo", name or "", schema is None

    def find(self, son, statement: _Statement, kinds: set[str], *, types: bool = False) -> _Object | None:
        found = self.target(son, statement)
        if found is None:
            return None
        database, schema, name, _ = found
        identity = (names.TYPE_PREFIX if types else "") + f"{database}.{schema}.{name}"
        obj = self.objects.get(self.key(identity, database))
        if obj is None or obj.kind not in kinds:
            raise FolderExtractionError(f"{statement.where}: {statement.kind} names {identity}, which no CREATE in "
                                        f"the folders makes")
        return obj

    def skip(self, statement: _Statement, reason: str) -> None:
        self.not_replayed.append({"file": statement.file.label, "line": statement.line, "statement": statement.kind,
                                  "reason": reason})

    def create(self, identity: str, kind: str, database: str, schema: str | None, name: str, statement: _Statement,
               schema_assumed: bool) -> _Object:
        key = self.key(identity, database)
        if key in self.objects:
            raise DuplicateCreate(identity, self.objects[key].where[0], statement.where)
        obj = _Object(identity, kind, database, schema, name, [statement.where])
        if schema_assumed:
            obj.assumed.append({"fact": "name_parts", "property": "schema", "value": schema, "reason": DEFAULT_SCHEMA})
        self.objects[key] = obj
        if schema is not None:
            self.schemas[database].add(schema)
        return obj

    def user_type(self, database: str) -> Callable[[list[str | None]], dict | None]:
        def lookup(parts: list[str | None]) -> dict | None:
            db = _canonical_database(parts[1] or database, self.databases) or database
            identity = f"{names.TYPE_PREFIX}{db}.{parts[2] or 'dbo'}.{parts[3]}"
            obj = self.objects.get(self.key(identity, db))
            if obj is None:
                return None
            if obj.kind == "table_type":
                return {"system_type": "table", "max_length": -1, "precision": 0, "scale": 0, "user_type": obj.identity}
            if obj.data.get("clr"):
                return {"system_type": "clr_type", "max_length": -1, "precision": 0, "scale": 0,
                        "user_type": obj.identity}
            base = obj.data["base"]
            return {"system_type": base["system_type"], "max_length": base["max_length"],
                    "precision": base["precision"], "scale": base["scale"], "collatable": base["collatable"],
                    "type_nullable": obj.data["nullable"], "user_type": obj.identity}
        return lookup

    # ------------------------------------------------------------ the replay

    def replay(self, statements: list[_Statement]) -> None:
        """Every CREATE statement (CREATE INDEX and CREATE OR ALTER among them) in file-name
        order, then every ALTER statement in file-name order (spec section 3)."""
        creates, alters = [], []
        for s in statements:
            if s.kind in CREATES or s.kind in INDEXES or s.kind in CREATE_OR_ALTER:
                creates.append(s)
            elif s.kind in ALTERS or _procedure(s.fragment) == TRIGGER_ORDER:
                alters.append(s)
            elif s.kind.startswith("Drop"):
                self.skip(s, DROP_REASON)
            elif s.kind in NOT_REPLAYED:
                self.skip(s, NOT_REPLAYED[s.kind])
            elif _procedure(s.fragment) in EXTENDED_PROPERTY:
                self.skip(s, "an extended property, which the catalog does not hold")
            elif s.kind.startswith("Set") or s.kind in SESSION:
                continue
            else:
                self.skip(s, "not data definition the replay applies")
        for s in creates:
            if s.database not in self.databases:
                self.skip(s, f"runs in database {s.database}, which this extraction does not read")
            elif s.kind in INDEXES:
                self.create_index(s)
            else:
                getattr(self, "create_" + s.kind)(s)
        for s in alters:
            if s.database not in self.databases:
                self.skip(s, f"runs in database {s.database}, which this extraction does not read")
                continue
            if _procedure(s.fragment) == TRIGGER_ORDER:
                self.trigger_order(s)
            else:
                getattr(self, "alter_" + s.kind)(s)

    # ------------------------------------------------------------ tables and table types

    def create_CreateSchemaStatement(self, s: _Statement) -> None:
        self.schemas[s.database].add(str(s.fragment.Name.Value))
        if s.fragment.StatementList is not None and s.fragment.StatementList.Statements.Count:
            self.skip(s, "statements inside CREATE SCHEMA, which the replay does not apply")

    def create_CreateTableStatement(self, s: _Statement) -> None:
        if s.fragment.SelectStatement is not None or s.fragment.Definition is None:
            self.skip(s, "a table created from a query, whose shape a folder cannot give")
            return
        found = self.target(s.fragment.SchemaObjectName, s)
        if found is None:
            return
        database, schema, name, assumed = found
        table = self.create(f"{database}.{schema}.{name}", "table", database, schema, name, s, assumed)
        self.new_table(table)
        self.add_elements(table, s.fragment.Definition, s)

    def create_CreateTypeTableStatement(self, s: _Statement) -> None:
        found = self.target(s.fragment.Name, s)
        if found is None:
            return
        database, schema, name, assumed = found
        table = self.create(f"{names.TYPE_PREFIX}{database}.{schema}.{name}", "table_type", database, schema, name, s,
                            assumed)
        self.new_table(table)
        self.add_elements(table, s.fragment.Definition, s)

    def create_CreateTypeUddtStatement(self, s: _Statement) -> None:
        found = self.target(s.fragment.Name, s)
        if found is None:
            return
        database, schema, name, assumed = found
        obj = self.create(f"{names.TYPE_PREFIX}{database}.{schema}.{name}", "user_type", database, schema, name, s,
                          assumed)
        base, unknown = type_shape(s.fragment.DataType, s.text, self.user_type(database))
        constraint = s.fragment.NullableConstraint
        obj.data.update(base=base, nullable=True if constraint is None else bool(constraint.Nullable), clr=False)
        obj.assumed += [{"fact": "base_type", **u} for u in unknown]

    def create_CreateTypeUdtStatement(self, s: _Statement) -> None:
        found = self.target(s.fragment.Name, s)
        if found is None:
            return
        database, schema, name, assumed = found
        obj = self.create(f"{names.TYPE_PREFIX}{database}.{schema}.{name}", "user_type", database, schema, name, s,
                          assumed)
        obj.data.update(clr=True)

    def new_table(self, table: _Object) -> None:
        table.data.update(columns=[], primary_key=None, unique=[], checks=[], foreign_keys=[], unique_indexes=[],
                          clustered=False)

    def add_elements(self, table: _Object, definition, s: _Statement, *, trusted: bool = True) -> None:
        for cd in definition.ColumnDefinitions:
            self.add_column(table, cd, s)
        for c in definition.TableConstraints:
            self.add_constraint(table, c, s, None, trusted)
        for index in (definition.Indexes or []):
            if index.Unique:
                columns = [self.sorted_column(table, c, s) for c in index.Columns]
                clustered = "CLUSTERED" if index.IndexType is not None and str(
                    index.IndexType.IndexTypeKind) == "Clustered" else "NONCLUSTERED"
                table.data["unique_indexes"].append({"name": _id(index.Name), "columns": columns,
                                                     "included": [self.column_name(table, _id(i)) for i in
                                                                  (index.IncludeColumns or [])],
                                                     "filter": (_text(s.text, index.FilterPredicate)
                                                                if index.FilterPredicate is not None else None),
                                                     "index_type": clustered, "enabled": True})
        table.touched(s)

    def column_name(self, table: _Object, name: str | None) -> str:
        """A column named in a constraint, as the table declares it."""
        fold = (lambda x: x) if self.databases[table.database].case_sensitive else str.casefold
        for c in table.data["columns"]:
            if fold(c["name"]) == fold(name or ""):
                return c["name"]
        raise FolderExtractionError(f"{table.identity}: no column {name} (in {table.where[-1]})")

    def sorted_column(self, table: _Object, column_with_order, s: _Statement) -> dict:
        ids = column_with_order.Column.MultiPartIdentifier.Identifiers
        return {"name": self.column_name(table, str(ids[ids.Count - 1].Value)),
                "descending": str(column_with_order.SortOrder) == "Descending"}

    def add_column(self, table: _Object, cd, s: _Statement) -> None:
        name = str(cd.ColumnIdentifier.Value)
        if any(c["name"].casefold() == name.casefold() for c in table.data["columns"]):
            raise FolderExtractionError(f"{s.where}: {table.identity} already has a column {name}")
        identity = None
        if cd.IdentityOptions is not None:
            o = cd.IdentityOptions
            identity = (_int(s.text, o.IdentitySeed) if o.IdentitySeed is not None else 1,
                        _int(s.text, o.IdentityIncrement) if o.IdentityIncrement is not None else 1)
        explicit = None
        column = {"name": name, "type": cd.DataType, "text": s.text, "collation": _id(cd.Collation),
                  "identity": identity, "computed": (_text(s.text, cd.ComputedColumnExpression)
                                                     if cd.ComputedColumnExpression is not None else None),
                  "persisted": bool(cd.IsPersisted), "default": None, "forced_not_null": False,
                  "null_default": s.null_default}
        if cd.DefaultConstraint is not None:
            column["default"] = {"name": _id(cd.DefaultConstraint.ConstraintIdentifier),
                                 "definition": _text(s.text, cd.DefaultConstraint.Expression)}
        table.data["columns"].append(column)
        for c in cd.Constraints:
            if _kind(c) == "NullableConstraintDefinition":
                explicit = bool(c.Nullable)
            else:
                self.add_constraint(table, c, s, name, True)
        column["nullable"] = explicit

    def add_constraint(self, table: _Object, c, s: _Statement, column: str | None, trusted: bool) -> None:
        kind, name = _kind(c), _id(getattr(c, "ConstraintIdentifier", None))
        if kind == "UniqueConstraintDefinition":
            columns = ([self.sorted_column(table, x, s) for x in c.Columns] if c.Columns.Count else
                       [{"name": column, "descending": False}])
            if c.IndexType is not None and str(c.IndexType.IndexTypeKind) in ("Clustered", "NonClustered"):
                clustered = str(c.IndexType.IndexTypeKind) == "Clustered"
            elif c.Clustered is not None:
                clustered = bool(c.Clustered)
            else:   # a primary key is clustered unless the table already has a clustered index
                clustered = bool(c.IsPrimaryKey) and not table.data["clustered"]
            table.data["clustered"] = table.data["clustered"] or clustered
            entry = {"name": name, "columns": columns, "index_type": "CLUSTERED" if clustered else "NONCLUSTERED"}
            if c.IsPrimaryKey:
                if table.data["primary_key"] is not None:
                    raise FolderExtractionError(f"{s.where}: {table.identity} already has a primary key")
                table.data["primary_key"] = entry
                for col in table.data["columns"]:
                    if col["name"] in {x["name"] for x in columns}:
                        col["forced_not_null"] = True
                fact = "primary_key"
            else:
                table.data["unique"].append(entry)
                fact = "unique_constraints"
        elif kind == "CheckConstraintDefinition":
            definition = _text(s.text, c.CheckCondition)
            table.data["checks"].append({"name": name, "definition": definition,
                                         "column": column or self.only_column(table, c.CheckCondition),
                                         "enabled": True, "trusted": trusted})
            fact = "check_constraints"
        elif kind == "ForeignKeyConstraintDefinition":
            columns = [self.column_name(table, _id(x)) for x in c.Columns] if c.Columns.Count else [column]
            table.data["foreign_keys"].append({
                "name": name, "columns": columns, "target": (c.ReferenceTableName, s),
                "referenced_columns": [_id(x) for x in c.ReferencedTableColumns],
                "on_delete": _action(c.DeleteAction), "on_update": _action(c.UpdateAction),
                "enabled": True, "trusted": trusted})
            fact = "foreign_keys"
        elif kind == "DefaultConstraintDefinition":
            target = column or self.column_name(table, _id(c.Column))
            col = next(x for x in table.data["columns"] if x["name"] == target)
            col["default"] = {"name": name, "definition": _text(s.text, c.Expression)}
            if name is None:
                col["default_unnamed"] = True
            return
        else:
            self.skip(s, f"a {kind} on {table.identity}, which the replay does not apply")
            return
        if name is None:
            table.assumed.append({"fact": fact, "property": "name", "value": None, "reason": NAMED_BY_SERVER,
                                  "where": s.where})

    def only_column(self, table: _Object, condition) -> str | None:
        """SQL Server records the column of a table-level check that names exactly one."""
        written: set[str] = set()

        def visit(f) -> None:
            if _kind(f) == "ColumnReferenceExpression" and f.MultiPartIdentifier is not None:
                ids = f.MultiPartIdentifier.Identifiers
                written.add(str(ids[ids.Count - 1].Value))
        parser.walk(condition, visit)
        try:
            found = {self.column_name(table, name) for name in written}
        except FolderExtractionError:
            return None
        return found.pop() if len(found) == 1 else None

    def create_index(self, s: _Statement) -> None:
        f = s.fragment
        if not f.Unique:
            self.skip(s, NOT_UNIQUE)
            if f.Clustered is not None and bool(f.Clustered):
                table = self.find(f.OnName, s, {"table", "view"})
                if table is not None:
                    table.data["clustered"] = True
            return
        table = self.find(f.OnName, s, {"table", "view"})
        if table is None:
            return
        table.data.setdefault("unique_indexes", [])
        table.data.setdefault("columns", [])
        clustered = f.Clustered is not None and bool(f.Clustered)
        columns = [self.index_column(table, c, s) for c in f.Columns]
        table.data["unique_indexes"].append({
            "name": _id(f.Name), "columns": columns,
            "included": [self.index_include(table, c) for c in (f.IncludeColumns or [])],
            "filter": _text(s.text, f.FilterPredicate) if f.FilterPredicate is not None else None,
            "index_type": "CLUSTERED" if clustered else "NONCLUSTERED", "enabled": True})
        table.touched(s)

    def index_column(self, table: _Object, c, s: _Statement) -> dict:
        ids = c.Column.MultiPartIdentifier.Identifiers
        name = str(ids[ids.Count - 1].Value)
        return {"name": self.column_name(table, name) if table.kind == "table" else name,
                "descending": str(c.SortOrder) == "Descending"}

    def index_include(self, table: _Object, c) -> str:
        ids = c.MultiPartIdentifier.Identifiers
        name = str(ids[ids.Count - 1].Value)
        return self.column_name(table, name) if table.kind == "table" else name

    # ------------------------------------------------------------ ALTER TABLE

    def alter_AlterTableAddTableElementStatement(self, s: _Statement) -> None:
        table = self.find(s.fragment.SchemaObjectName, s, {"table"})
        if table is not None:
            trusted = str(s.fragment.ExistingRowsCheckEnforcement) != "NoCheck"
            self.add_elements(table, s.fragment.Definition, s, trusted=trusted)

    def alter_AlterTableAlterColumnStatement(self, s: _Statement) -> None:
        f = s.fragment
        table = self.find(f.SchemaObjectName, s, {"table"})
        if table is None:
            return
        name = self.column_name(table, _id(f.ColumnIdentifier))
        column = next(c for c in table.data["columns"] if c["name"] == name)
        if f.DataType is None:
            self.skip(s, "an ALTER COLUMN that changes no type, which the replay does not apply")
            return
        option = str(f.AlterTableAlterColumnOption)
        column.update(type=f.DataType, text=s.text, collation=_id(f.Collation), null_default=s.null_default,
                      nullable=True if option == "Null" else False if option == "NotNull" else None)
        table.touched(s)

    def alter_AlterTableDropTableElementStatement(self, s: _Statement) -> None:
        table = self.find(s.fragment.SchemaObjectName, s, {"table"})
        if table is None:
            return
        for element in s.fragment.AlterTableDropTableElements:
            name, kind = _id(element.Name), str(element.TableElementType)
            if kind == "Column":
                target = self.column_name(table, name)
                table.data["columns"] = [c for c in table.data["columns"] if c["name"] != target]
            elif kind in ("Constraint", "NotSpecified"):
                if not self.drop_constraint(table, name):
                    if element.IsIfExists:
                        continue
                    raise FolderExtractionError(f"{s.where}: {table.identity} has no constraint {name} to drop")
            else:
                self.skip(s, f"dropping a {kind}, which the replay does not apply")
        table.touched(s)

    def drop_constraint(self, table: _Object, name: str | None) -> bool:
        fold = (lambda x: x) if self.databases[table.database].case_sensitive else str.casefold
        d = table.data
        if d["primary_key"] and d["primary_key"]["name"] and fold(d["primary_key"]["name"]) == fold(name or ""):
            d["primary_key"] = None
            return True
        for key in ("unique", "checks", "foreign_keys"):
            kept = [c for c in d[key] if not (c["name"] and fold(c["name"]) == fold(name or ""))]
            if len(kept) != len(d[key]):
                d[key] = kept
                return True
        for c in d["columns"]:
            if c["default"] and c["default"]["name"] and fold(c["default"]["name"]) == fold(name or ""):
                c["default"] = None
                return True
        return False

    def alter_AlterTableConstraintModificationStatement(self, s: _Statement) -> None:
        f = s.fragment
        table = self.find(f.SchemaObjectName, s, {"table"})
        if table is None:
            return
        wanted = {str(n.Value).casefold() for n in f.ConstraintNames}
        enforce = str(f.ConstraintEnforcement) == "Check"
        recheck = str(f.ExistingRowsCheckEnforcement) == "Check"
        constraints = table.data["checks"] + table.data["foreign_keys"]
        missing = wanted - {(c["name"] or "").casefold() for c in constraints}
        if missing and not f.All:
            raise FolderExtractionError(f"{s.where}: {table.identity} has no check or foreign key named "
                                        f"{', '.join(sorted(missing))}")
        for c in constraints:
            if f.All or (c["name"] or "").casefold() in wanted:
                c["enabled"] = enforce
                c["trusted"] = enforce and (recheck or c["trusted"])
        table.touched(s)

    def alter_AlterTableTriggerModificationStatement(self, s: _Statement) -> None:
        f = s.fragment
        table = self.find(f.SchemaObjectName, s, {"table"})
        if table is None:
            return
        pool = [o for o in self.objects.values() if o.kind == "trigger" and
                o.data["table_key"] == self.key(table.identity, table.database)]
        self.set_enabled(pool, f.All, {str(n.Value).casefold() for n in f.TriggerNames},
                         str(f.TriggerEnforcement) == "Enable", s)

    def alter_EnableDisableTriggerStatement(self, s: _Statement) -> None:
        f = s.fragment
        enable = str(f.TriggerEnforcement) == "Enable"
        if str(f.TriggerObject.TriggerScope) == "Database":
            pool = [o for o in self.objects.values() if o.kind == "database_trigger" and o.database == s.database]
        else:
            table = self.find(f.TriggerObject.Name, s, {"table", "view"})
            if table is None:
                return
            pool = [o for o in self.objects.values() if o.kind == "trigger" and
                    o.data["table_key"] == self.key(table.identity, table.database)]
        self.set_enabled(pool, f.All, {(_parts(n)[3] or "").casefold() for n in f.TriggerNames}, enable, s)

    def set_enabled(self, pool: list[_Object], every: bool, named: set[str], enabled: bool, s: _Statement) -> None:
        missing = named - {o.name.casefold() for o in pool}
        if missing and not every:
            raise FolderExtractionError(f"{s.where}: no CREATE TRIGGER in the folders makes "
                                        f"{', '.join(sorted(missing))} there")
        for t in pool:
            if every or t.name.casefold() in named:
                t.data["enabled"] = (enabled, s.where)
                t.touched(s)

    def trigger_order(self, s: _Statement) -> None:
        names_ = ("triggername", "order", "stmttype", "namespace")
        args: dict[str, Any] = {}
        for index, p in enumerate(s.fragment.ExecuteSpecification.ExecutableEntity.Parameters):
            key = str(p.Variable.Name).casefold().lstrip("@") if p.Variable is not None else names_[index]
            args[key] = str(p.ParameterValue.Value) if _kind(p.ParameterValue) == "StringLiteral" else None
        if None in (args.get("triggername"), args.get("order"), args.get("stmttype")):
            self.skip(s, "sp_settriggerorder with an argument that is not a literal")
            return
        parts = names.split_name(args["triggername"])
        candidates = [o for o in self.objects.values() if o.kind == "trigger" and o.database == s.database
                      and o.name.casefold() == parts[-1].casefold()
                      and (len(parts) < 2 or (o.schema or "").casefold() == parts[-2].casefold())]
        if len(candidates) != 1:
            raise FolderExtractionError(f"{s.where}: sp_settriggerorder names {args['triggername']}, which no CREATE "
                                        "TRIGGER in the folders makes")
        trigger = candidates[0]
        trigger.data["firing_order"][args["stmttype"].upper()] = (args["order"].casefold(), s.where)
        trigger.touched(s)

    # ------------------------------------------------------------ modules

    def create_CreateViewStatement(self, s: _Statement) -> None:
        self.module(s, s.fragment.SchemaObjectName, "view", "V", replace=False)

    def create_CreateProcedureStatement(self, s: _Statement) -> None:
        clr = s.fragment.MethodSpecifier is not None
        self.module(s, s.fragment.ProcedureReference.Name, "clr_procedure" if clr else "procedure",
                    "PC" if clr else "P", replace=False)

    def create_CreateFunctionStatement(self, s: _Statement) -> None:
        self.module(s, s.fragment.Name, *_function_kind(s.fragment), replace=False)

    def create_CreateTriggerStatement(self, s: _Statement) -> None:
        self.trigger(s, replace=False)

    def alter_AlterViewStatement(self, s: _Statement) -> None:
        self.module(s, s.fragment.SchemaObjectName, "view", "V", replace=True)

    def alter_AlterProcedureStatement(self, s: _Statement) -> None:
        clr = s.fragment.MethodSpecifier is not None
        self.module(s, s.fragment.ProcedureReference.Name, "clr_procedure" if clr else "procedure",
                    "PC" if clr else "P", replace=True)

    def alter_AlterFunctionStatement(self, s: _Statement) -> None:
        self.module(s, s.fragment.Name, *_function_kind(s.fragment), replace=True)

    def alter_AlterTriggerStatement(self, s: _Statement) -> None:
        self.trigger(s, replace=True)

    create_CreateOrAlterViewStatement = create_CreateViewStatement
    create_CreateOrAlterProcedureStatement = create_CreateProcedureStatement
    create_CreateOrAlterFunctionStatement = create_CreateFunctionStatement
    create_CreateOrAlterTriggerStatement = create_CreateTriggerStatement

    def module(self, s: _Statement, son, kind: str, code: str, *, replace: bool,
               identity: str | None = None, schema: str | None = None) -> _Object | None:
        if identity is None:
            found = self.target(son, s)
            if found is None:
                return None
            database, schema, name, assumed = found
            identity = f"{database}.{schema}.{name}"
        else:
            database, name, assumed = s.database, identity.split(".")[-1], False
        key = self.key(identity, database)
        if replace and key in self.objects:
            obj = self.objects[key]
            obj.where.append(s.where)
            obj.kind = kind
        elif replace:
            raise FolderExtractionError(f"{s.where}: {s.kind} of {identity}, which no CREATE in the folders makes")
        else:
            obj = self.create(identity, kind, database, schema, name, s, assumed)
        obj.data.update(statement=s, code=code, source=s.source())
        return obj

    def trigger(self, s: _Statement, *, replace: bool) -> None:
        f = s.fragment
        scope = str(f.TriggerObject.TriggerScope)
        events = sorted(_snake(str(a.TriggerActionType)) if str(a.TriggerActionType) != "Event" else
                        _snake(str(a.EventTypeGroup.EventType if _kind(a.EventTypeGroup) == "EventTypeContainer"
                                   else a.EventTypeGroup.EventGroup))
                        for a in f.TriggerActions)
        clr = f.MethodSpecifier is not None
        if scope == "AllServer":
            self.skip(s, "a server trigger, which the catalog does not hold")
            return
        if scope == "Database":
            name = _parts(f.Name)[3] or ""
            obj = self.module(s, f.Name, "database_trigger", "TR", replace=replace,
                              identity=f"{s.database}.{name}")
            if obj is not None:
                obj.data.update(events=events, timing="AFTER", table_key=None, table=None, clr=clr)
                obj.data.setdefault("enabled", (True, None))
                obj.data.setdefault("firing_order", {})
            return
        table_target = self.target(f.TriggerObject.Name, s)
        if table_target is None:
            return
        database, table_schema, table_name, table_assumed = table_target
        own = _parts(f.Name)
        schema = own[2] or table_schema     # a trigger is created in its table's schema
        obj = self.module(s, None, "clr_trigger" if clr else "trigger", "TA" if clr else "TR", replace=replace,
                          identity=f"{database}.{schema}.{own[3]}", schema=schema)
        if obj is None:
            return
        table_identity = f"{database}.{table_schema}.{table_name}"
        obj.data.update(events=events, timing={"InsteadOf": "INSTEAD OF"}.get(str(f.TriggerType), "AFTER"),
                        table=table_identity, table_key=self.key(table_identity, database), clr=clr)
        obj.data.setdefault("enabled", (True, None))
        obj.data.setdefault("firing_order", {})
        if table_assumed and own[2] is None:
            obj.assumed.append({"fact": "table", "property": "schema", "value": table_schema,
                                "reason": DEFAULT_SCHEMA})

    # ------------------------------------------------------------ other objects

    def create_CreateSynonymStatement(self, s: _Statement) -> None:
        found = self.target(s.fragment.Name, s)
        if found is None:
            return
        database, schema, name, assumed = found
        obj = self.create(f"{database}.{schema}.{name}", "synonym", database, schema, name, s, assumed)
        obj.data["base_object"] = [p for p in _parts(s.fragment.ForName) if p is not None]

    def create_CreateSequenceStatement(self, s: _Statement) -> None:
        found = self.target(s.fragment.Name, s)
        if found is None:
            return
        database, schema, name, assumed = found
        obj = self.create(f"{database}.{schema}.{name}", "sequence", database, schema, name, s, assumed)
        options: dict[str, Any] = {}
        for o in s.fragment.SequenceOptions:
            kind = str(o.OptionKind)
            if kind == "As":
                options["type"] = type_shape(o.DataType, s.text, self.user_type(database))[0]
            elif kind == "Cycle":
                options["cycles"] = not o.NoValue
            elif kind == "Cache":   # NO CACHE, CACHE, or CACHE n
                options["cache"] = (False, None) if o.NoValue else (
                    True, _int(s.text, o.OptionValue) if _kind(o) == "ScalarExpressionSequenceOption" else None)
            elif not o.NoValue:     # START WITH, INCREMENT BY, MINVALUE, MAXVALUE
                options[kind] = _int(s.text, o.OptionValue)
        obj.data["options"] = options

    # ------------------------------------------------------------ building the catalog objects

    def build(self) -> list[CatalogObject]:
        out = []
        for obj in sorted(self.objects.values(), key=lambda o: o.identity):
            builder = {"table": self.table_object, "table_type": self.table_type_object,
                       "user_type": self.user_type_object, "synonym": self.synonym_object,
                       "sequence": self.sequence_object}.get(obj.kind, self.module_object)
            out.append(builder(obj))
        return out

    def name_parts(self, obj: _Object) -> Fact:
        return Fact("skeleton", "name_parts", [obj.database, obj.schema, obj.name], obj.provenance)

    def assumed_fact(self, obj: _Object, extra: list[dict] | None = None) -> list[Fact]:
        values = obj.assumed + (extra or [])
        if not values:
            return []
        return [Fact("skeleton", "assumed_values", values,
                     Provenance("assumed", "SQL Server's documented defaults and the settings given, where the files "
                                           "do not say"))]

    def columns(self, obj: _Object) -> tuple[list[dict], list[dict]]:
        """The columns as the catalog holds them, and what was assumed about each."""
        context = self.databases[obj.database]
        out, assumed = [], []
        for c in obj.data["columns"]:
            name = c["name"]
            computed = c["computed"] is not None
            shape, unknown = ((({"declared_type": None, "system_type": None, "max_length": None, "precision": None,
                                 "scale": None, "collatable": False, "type_nullable": None, "user_type": None}), [])
                              if computed and c["type"] is None else
                              type_shape(c["type"], c["text"], self.user_type(obj.database)))
            assumed += [{"fact": "columns", "column": name, **u} for u in unknown]
            if computed:
                assumed.append({"fact": "columns", "column": name, "property": "declared_type", "value": None,
                                "reason": "a computed column's type is worked out by SQL Server from its expression"})
            if c["nullable"] is not None:
                nullable = c["nullable"]
            elif c["forced_not_null"] or c["identity"] or shape["system_type"] == "timestamp":
                nullable = False
            elif shape["type_nullable"] is not None:
                nullable = shape["type_nullable"]
            elif computed:
                nullable = True
                assumed.append({"fact": "columns", "column": name, "property": "nullable", "value": True,
                                "reason": COMPUTED_NULLABLE})
            elif c["null_default"] is not None:
                nullable = c["null_default"].value
            else:
                nullable = True
                assumed.append({"fact": "columns", "column": name, "property": "nullable", "value": True,
                                "reason": NULL_DEFAULT})
            if c["forced_not_null"]:
                nullable = False
            collation = c["collation"]
            if collation is None and shape["collatable"]:
                collation = context.collation
                assumed.append({"fact": "columns", "column": name, "property": "collation", "value": collation,
                                "reason": COLLATION_DEFAULT})
            column = {"name": name, **{k: shape[k] for k in ("declared_type", "system_type", "max_length",
                                                              "precision", "scale")},
                      "nullable": nullable, "collation": collation}
            if obj.kind == "table":
                column.update(identity=c["identity"] is not None, computed=computed,
                              user_type=shape["user_type"],
                              default=c["default"] and {"name": c["default"]["name"],
                                                        "definition": c["default"]["definition"]})
                if c["default"] is not None and c["default"]["name"] is None:
                    assumed.append({"fact": "columns", "column": name, "property": "default.name", "value": None,
                                    "reason": NAMED_BY_SERVER})
                if c["identity"] is not None:
                    column["identity_seed"], column["identity_increment"] = c["identity"]
                if computed:
                    column["computed_expression"] = c["computed"]
                    column["computed_persisted"] = c["persisted"]
            out.append(column)
        return out, assumed

    def constraint_facts(self, obj: _Object, p: Provenance) -> list[Fact]:
        d = obj.data
        by_name = lambda c: (c["name"] is None, c["name"] or "")  # noqa: E731
        fks = []
        for fk in sorted(d["foreign_keys"], key=by_name):
            son, statement = fk["target"]
            target = self.target(son, statement)
            references, referenced = None, fk["referenced_columns"]
            if target is not None:
                database, schema, name, _ = target
                references = f"{database}.{schema}.{name}"
                found = self.objects.get(self.key(references, database))
                if found is None:
                    obj.assumed.append({"fact": "foreign_keys", "property": "references", "value": references,
                                        "reason": "the referenced table is not created in the folders"})
                else:
                    references = found.identity
                    if not referenced:
                        key = found.data.get("primary_key")
                        referenced = [c["name"] for c in key["columns"]] if key else []
            fks.append({"name": fk["name"], "columns": fk["columns"], "references": references,
                        "referenced_columns": referenced, "on_delete": fk["on_delete"], "on_update": fk["on_update"],
                        "enabled": fk["enabled"], "trusted": fk["trusted"]})
        return [Fact("skeleton", "primary_key", d["primary_key"], p),
                Fact("skeleton", "unique_constraints", sorted(d["unique"], key=by_name), p),
                Fact("skeleton", "check_constraints", sorted(d["checks"], key=by_name), p),
                Fact("skeleton", "foreign_keys", fks, p),
                Fact("skeleton", "unique_indexes", d["unique_indexes"], p)]

    def table_object(self, obj: _Object) -> CatalogObject:
        p = obj.provenance
        columns, assumed = self.columns(obj)
        constraints = self.constraint_facts(obj, p)
        marked = []
        for index, c in enumerate(columns):
            for key, text in (("default", (c["default"] or {}).get("definition")),
                              ("computed_expression", c.get("computed_expression"))):
                marked += _marked("columns", secrets.in_text(text or ""), index, key)
        checks = next(f for f in constraints if f.name == "check_constraints").value
        for index, c in enumerate(checks):
            marked += _marked("check_constraints", secrets.in_text(c["definition"] or ""), index, "definition")
        facts = [Fact(SOURCE, "object_type", "U", p), self.name_parts(obj),
                 Fact("skeleton", "columns", columns, p),
                 Fact("skeleton", "not_null", [c["name"] for c in columns if not c["nullable"]], p),
                 *constraints, Fact("skeleton", "secrets", marked, p)]
        return CatalogObject(obj.identity, "table", tuple(facts + self.assumed_fact(obj, assumed)))

    def table_type_object(self, obj: _Object) -> CatalogObject:
        p = obj.provenance
        columns, assumed = self.columns(obj)
        facts = [Fact("skeleton", "columns", columns, p), *self.constraint_facts(obj, p)]
        return CatalogObject(obj.identity, "table_type", tuple(facts + self.assumed_fact(obj, assumed)))

    def user_type_object(self, obj: _Object) -> CatalogObject:
        p = obj.provenance
        if obj.data["clr"]:
            return CatalogObject(obj.identity, "user_type", (Fact("skeleton", "clr_type", True, p),))
        base = obj.data["base"]
        collation = None
        extra = []
        if base["collatable"]:
            collation = self.databases[obj.database].collation
            extra.append({"fact": "base_type", "property": "collation", "value": collation,
                          "reason": COLLATION_DEFAULT})
        fact = Fact("skeleton", "base_type", {"system_type": base["system_type"], "max_length": base["max_length"],
                                              "precision": base["precision"], "scale": base["scale"],
                                              "nullable": obj.data["nullable"], "collation": collation}, p)
        return CatalogObject(obj.identity, "user_type", (fact, *self.assumed_fact(obj, extra)))

    def synonym_object(self, obj: _Object) -> CatalogObject:
        p = obj.provenance
        return CatalogObject(obj.identity, "synonym", (Fact(SOURCE, "object_type", "SN", p), self.name_parts(obj),
                                                       Fact("skeleton", "base_object", obj.data["base_object"], p),
                                                       *self.assumed_fact(obj)))

    def sequence_object(self, obj: _Object) -> CatalogObject:
        p = obj.provenance
        o = obj.data["options"]
        shape = o.get("type", {"declared_type": "bigint", "system_type": "bigint", "precision": 19})
        low, high = _value_range(shape)
        increment = o.get("Increment", 1)
        minimum, maximum = o.get("MinValue", low), o.get("MaxValue", high)
        cached, size = o.get("cache", (True, None))
        value = {"type": shape["declared_type"], "start": o.get("Start", minimum if increment > 0 else maximum),
                 "increment": increment, "minimum": minimum, "maximum": maximum, "cycles": o.get("cycles", False),
                 "cached": cached, "cache_size": size}
        written = {"type": "type", "start": "Start", "increment": "Increment", "minimum": "MinValue",
                   "maximum": "MaxValue", "cycles": "cycles", "cached": "cache", "cache_size": "cache"}
        obj.assumed += [{"fact": "sequence", "property": key, "value": value[key], "reason": SEQUENCE_DEFAULT}
                        for key, option in written.items() if option not in o]
        return CatalogObject(obj.identity, "sequence", (Fact(SOURCE, "object_type", "SO", p), self.name_parts(obj),
                                                        Fact("skeleton", "sequence", value, p),
                                                        *self.assumed_fact(obj)))

    def module_object(self, obj: _Object) -> CatalogObject:
        s: _Statement = obj.data["statement"]
        p = obj.provenance
        f = s.fragment
        text = obj.data["source"]
        triggers = ("trigger", "clr_trigger", "database_trigger")
        # As from a server: sys.triggers, not sys.objects, describes a trigger.
        facts = [] if obj.kind in triggers else [Fact(SOURCE, "object_type", obj.data["code"], p)]
        if obj.kind != "database_trigger":
            facts.append(self.name_parts(obj))
        extra: list[dict] = []
        if obj.kind in triggers:
            enabled, _ = obj.data["enabled"]
            order = {e: obj.data["firing_order"].get(e, ("none", None))[0] for e in obj.data["events"]}
            if any(obj.data["firing_order"].get(e) is None for e in obj.data["events"]):
                extra.append({"fact": "firing_order", "property": "order", "value": "none", "reason": FIRING_ORDER})
            facts += [Fact("skeleton", "events", obj.data["events"], p), Fact("skeleton", "timing", obj.data["timing"], p),
                      Fact("skeleton", "firing_order", order, p), Fact("skeleton", "enabled", enabled, p)]
            if obj.data["table"] is not None:
                found = self.objects.get(obj.data["table_key"])
                facts.append(Fact("skeleton", "table", found.identity if found else obj.data["table"], p))
                if found is None:
                    extra.append({"fact": "table", "property": "table", "value": obj.data["table"],
                                  "reason": "the trigger's table is not created in the folders"})
        if obj.kind == "view":
            facts.append(Fact("skeleton", "unique_indexes", obj.data.get("unique_indexes", []), p))
        if obj.kind.startswith("clr_"):
            facts += [Fact("skeleton", "source_text", None, p), Fact("skeleton", "extractable", False, p),
                      Fact("skeleton", "not_extractable_reason", "a CLR module: it has no T-SQL source", p)]
        else:
            ansi, quoted = s.ansi_nulls, s.quoted_identifier
            facts += [Fact(SOURCE, "ansi_nulls", ansi.value, _setting_provenance(ansi, "ANSI_NULLS")),
                      Fact(SOURCE, "quoted_identifier", quoted.value,
                           _setting_provenance(quoted, "QUOTED_IDENTIFIER")),
                      Fact(SOURCE, "schema_bound", _has_option(f, "SchemaBinding"), p),
                      Fact("skeleton", "source_text", text, p), Fact("skeleton", "extractable", True, p),
                      Fact("skeleton", "secrets", _marked("source_text", secrets.in_text(text)), p)]
            if _has_option(f, "Encryption"):
                facts.append(Fact(SOURCE, "with_encryption", True, p))
        if obj.kind in ("view", "inline_function"):
            facts.append(Fact("skeleton", "result_shape_unavailable",
                              "a view's or inline function's columns come from a server's metadata; from a file they "
                              "are described in the sandbox (issue #41)", p))
        if obj.kind == "table_function":
            body = f.ReturnType.DeclareTableVariableBody
            shape = [{k: v for k, v in c.items() if k != "collation"} for c in self.declared_columns(obj, body, s)]
            facts.append(Fact("skeleton", "result_shape", shape, p))
        if obj.kind in ("procedure", "scalar_function", "inline_function", "table_function", "clr_procedure",
                        "clr_scalar_function", "clr_table_function"):
            facts += self.parameter_facts(obj, s, p)
        return CatalogObject(obj.identity, obj.kind, tuple(facts + self.assumed_fact(obj, extra)))

    def declared_columns(self, obj: _Object, body, s: _Statement) -> list[dict]:
        holder = _Object(obj.identity, "table_type", obj.database, obj.schema, obj.name, obj.where)
        self.new_table(holder)
        for cd in body.Definition.ColumnDefinitions:
            self.add_column(holder, cd, s)
        columns, assumed = self.columns(holder)
        obj.assumed += [{**a, "fact": "result_shape"} for a in assumed]
        return columns

    def parameter_facts(self, obj: _Object, s: _Statement, p: Provenance) -> list[Fact]:
        params = []
        for param in s.fragment.Parameters:
            shape, unknown = type_shape(param.DataType, s.text, self.user_type(obj.database))
            obj.assumed += [{"fact": "parameters", "parameter": str(param.VariableName.Value), **u} for u in unknown]
            modifier = str(param.Modifier)
            params.append({"name": str(param.VariableName.Value),
                           **{k: shape[k] for k in ("declared_type", "system_type", "max_length", "precision",
                                                     "scale")},
                           "direction": "output" if modifier == "Output" else "input",
                           "readonly": modifier == "ReadOnly", "nullable": True,
                           "default": _text(s.text, param.Value) if param.Value is not None else None})
        facts = [Fact("skeleton", "parameters", params, p)]
        if obj.kind in ("scalar_function", "clr_scalar_function"):
            shape, _ = type_shape(s.fragment.ReturnType.DataType, s.text, self.user_type(obj.database))
            facts.append(Fact("skeleton", "returns", {k: shape[k] for k in ("declared_type", "system_type",
                                                                            "max_length", "precision", "scale")}, p))
        return facts


def _action(value) -> str:
    return {"Cascade": "CASCADE", "SetNull": "SET_NULL", "SetDefault": "SET_DEFAULT"}.get(str(value), "NO_ACTION")


def _function_kind(f) -> tuple[str, str]:
    clr = f.MethodSpecifier is not None
    kind = _kind(f.ReturnType)
    if kind == "ScalarFunctionReturnType":
        return ("clr_scalar_function", "FS") if clr else ("scalar_function", "FN")
    if kind == "SelectFunctionReturnType":
        return "inline_function", "IF"
    return ("clr_table_function", "FT") if clr else ("table_function", "TF")


def _has_option(f, option: str) -> bool:
    for name in ("Options", "ViewOptions"):
        for o in (getattr(f, name, None) or []):
            if str(o.OptionKind) == option:
                return True
    return False


def _setting_provenance(setting: _Setting, name: str) -> Provenance:
    if setting.where is None:
        return Provenance("assumed", f"SET {name} {CLIENT_DEFAULT_ON}")
    return Provenance("file", f"{setting.where}: SET {name}")


def _value_range(shape: dict) -> tuple[int, int]:
    """The values a sequence's type holds: an integer type's range, or a decimal of scale 0's."""
    kind = shape["system_type"]
    if kind == "tinyint":
        return 0, 255
    bits = {"smallint": 16, "int": 32, "bigint": 64}.get(kind)
    if bits is not None:
        return -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
    if kind in ("decimal", "numeric"):
        return -(10 ** shape["precision"] - 1), 10 ** shape["precision"] - 1
    raise FolderExtractionError(f"a sequence of type {shape['declared_type']}, which SQL Server does not allow")


def _marked(fact: str, spans, row: int | None = None, column: str | None = None) -> list[dict]:
    return [{"fact": fact, **(dataclasses.replace(s, row=row, column=column) if row is not None else s).to_json()}
            for s in spans]


# ---------------------------------------------------------------------------- the whole extraction

@dataclasses.dataclass
class FolderResult:
    """What the folders gave: catalog objects, and what is resolved once every object of the
    extraction is known."""
    objects: list[CatalogObject]
    pending: list[tuple[str, Callable[[Snapshot], list[Fact]]]]   # identity -> its reference facts
    schemas: dict[str, set[str]]


def raw_identity(file: SourceFile) -> str:
    return f"raw:file:{file.label}"


def extract(files: list[SourceFile], databases: dict[str, DatabaseContext], *,
            live: bool) -> FolderResult:
    """The catalog objects the files give. `databases` holds every database of the
    extraction; those not `live` are built by replaying their data definition."""
    objects: list[CatalogObject] = []
    pending: list[tuple[str, Callable[[Snapshot], list[Fact]]]] = []
    roles: list[dict] = []
    superseded: list[dict] = []
    statements: list[_Statement] = []
    job_files: dict[str, str] = {}             # a scripted job's name -> where it is created
    replay = _Replay({d: c for d, c in databases.items() if not c.live})
    engagement = set(databases)
    for file in files:
        database = _canonical_database(file.folder.database, databases)
        if database is None:
            raise FolderExtractionError(f"the folder {file.folder.path} is for database {file.folder.database}, which "
                                        "this extraction does not read")
        p = Provenance("file", file.label)
        if file.text is None:
            roles.append({"file": file.label, "role": NOT_READ, "reason": "not a .sql or .dtsx file"})
            continue
        if file.suffix in PACKAGE_SUFFIXES:
            marked = secrets.in_package(file.text)
            objects.append(_raw(file, [s.to_json() for s in marked]))
            roles.append({"file": file.label, "role": PACKAGE})
            obj, resolve = _package(file, database, databases)
            objects.append(obj)
            pending.append((obj.identity, resolve))
            continue
        objects.append(_raw(file, [s.to_json() for s in secrets.in_text(file.text)]))
        context = databases[database]
        try:
            parsed = parser.parse(file.label, file.text, context.compatibility_level, True)
        except parser.ParseFailed as failed:
            roles.append({"file": file.label, "role": DID_NOT_PARSE, "reason": str(failed)})
            continue
        role = role_of(parsed)
        roles.append({"file": file.label, "role": role})
        if role == JOB_SCRIPT:
            if live:
                superseded.append({"file": file.label, "reason": "SQL Agent jobs are read from the live server's msdb"})
                continue
            try:
                made, unread = jobs.from_script(parsed)
            except jobs.DuplicateJob as twice:
                raise DuplicateCreate(f"agent_job:{twice.name}", file.label, file.label) from None
            for job in made:
                if job.name in job_files:
                    raise DuplicateCreate(f"agent_job:{job.name}", job_files[job.name], file.label)
                job_files[job.name] = f"{file.label} line {job.line}"
                objects.append(_scripted_job(job, file, unread))
        elif role == DATA_DEFINITION:
            found = statements_of(file, parsed, databases)
            if databases[database].live:
                superseded += [{"file": file.label, "line": s.line, "statement": s.kind,
                                "reason": f"database {database} is read from the live server, which is the source of "
                                          "its objects (ADR 0025)"} for s in found
                               if not s.kind.startswith("Set") and s.kind not in SESSION]
            else:
                statements += found
        else:
            obj = _batch(file, database, p)
            objects.append(obj)
            pending.append((obj.identity, _batch_references(obj.identity, parsed, database, databases, engagement)))
    replay.replay(statements)
    built = replay.build()
    objects += built
    for obj in built:
        if obj.has_fact("source_text") and obj.fact("source_text").value is not None:
            pending.append((obj.identity, _module_references(obj, databases, engagement)))
    objects.append(CatalogObject(FOLDERS_IDENTITY, "folder_extraction", (
        Fact("skeleton", "files", roles, Provenance("file", "every file in the folders")),
        Fact("skeleton", "not_replayed", replay.not_replayed, Provenance("file", "the data-definition files")),
        Fact("skeleton", "superseded", superseded, Provenance("file", "files beside a live server")),
        Fact("skeleton", "lost_without_a_server",
             [] if live else list(LOST_WITHOUT_A_SERVER), Provenance("file", "spec section 3")),
    )))
    return FolderResult(objects, pending, replay.schemas)


def _raw(file: SourceFile, marked: list[dict]) -> CatalogObject:
    p = Provenance("file", file.label)
    return CatalogObject(raw_identity(file), "raw_capture", (
        Fact("skeleton", "file", file.label, p), Fact("skeleton", "encoding", file.encoding, p),
        Fact("skeleton", "text", file.text, p), Fact("skeleton", "secrets", marked, p)))


SKIPPED_CROSS_CHECK = {"status": "skipped",
                       "reason": "parsed from a file: SQL Server's dependency metadata covers only objects read from a "
                                 "server"}


def _batch(file: SourceFile, database: str, p: Provenance) -> CatalogObject:
    text = file.text or ""
    return CatalogObject(f"batch:{file.label}", "query_batch", (
        Fact("skeleton", "file", file.label, p),
        Fact("skeleton", "database", database, Provenance("assumed", "the database of the folder the file is in, "
                                                                     "given by the operator")),
        Fact("skeleton", "source_text", text, p), Fact("skeleton", "extractable", True, p),
        Fact(SOURCE, "ansi_nulls", True, Provenance("assumed", f"SET ANSI_NULLS {CLIENT_DEFAULT_ON}")),
        Fact(SOURCE, "quoted_identifier", True, Provenance("assumed", f"SET QUOTED_IDENTIFIER {CLIENT_DEFAULT_ON}")),
        Fact("skeleton", "secrets", _marked("source_text", secrets.in_text(text)), p),
        Fact(SOURCE, "dependency_cross_check", SKIPPED_CROSS_CHECK, p)))


def _batch_references(identity: str, parsed: parser.Parsed, database: str, databases: dict[str, DatabaseContext],
                      engagement: set[str]) -> Callable[[Snapshot], list[Fact]]:
    def resolve(snapshot: Snapshot) -> list[Fact]:
        context = databases[database]
        where = resolution.context(database, "dbo", context.case_sensitive)
        found = resolution.work_out(parsed, snapshot, where, system_types=set(context.system_types),
                                    schemas={s.casefold() for s in context.schemas}, engagement=engagement)
        return [Fact("skeleton", "references", found, Provenance("file", f"{identity[len('batch:'):]}, parsed by "
                                                                         "ScriptDom"))]
    return resolve


def _module_references(obj: CatalogObject, databases: dict[str, DatabaseContext],
                       engagement: set[str]) -> Callable[[Snapshot], list[Fact]]:
    def resolve(snapshot: Snapshot) -> list[Fact]:
        database = obj.identity.split(".")[0]
        context = databases[database]
        p = Provenance("file", f"{obj.fact('source_text').provenance.detail}, parsed by ScriptDom")
        quoted = obj.fact("quoted_identifier", SOURCE).value
        try:
            parsed = parser.parse(obj.identity, obj.fact("source_text").value, context.compatibility_level, quoted)
        except parser.ParseFailed as failed:
            reason = f"the text did not parse: {failed}"
            return [Fact("skeleton", "references_unavailable", reason, p),
                    Fact(SOURCE, "dependency_cross_check", {"status": "not run", "reason": reason}, p)]
        schema = obj.identity.split(".")[1] if obj.identity.count(".") == 2 else "dbo"
        where = resolution.context(database, schema, context.case_sensitive)
        found = resolution.work_out(parsed, snapshot, where, system_types=set(context.system_types),
                                    schemas={s.casefold() for s in context.schemas}, engagement=engagement)
        return [Fact("skeleton", "references", found, p), Fact(SOURCE, "dependency_cross_check", SKIPPED_CROSS_CHECK, p)]
    return resolve


def _scripted_job(job: jobs.ScriptedJob, file: SourceFile, unread: list[dict]) -> CatalogObject:
    p = Provenance("file", f"{file.label} line {job.line}")
    v = job.values
    assumed = Provenance("assumed", "SQL Server sets it when the job is created")
    facts = [Fact("skeleton", "job_id", None, assumed),
             Fact("skeleton", "enabled", bool(v["enabled"]), p),
             Fact("skeleton", "description", v["description"], p),
             Fact("skeleton", "category", v["category_name"], p),
             Fact("skeleton", "start_step", v["start_step_id"], p),
             Fact("skeleton", "steps", job.steps, p),
             Fact("skeleton", "schedules", job.schedules, p),
             Fact("skeleton", "secrets", jobs.marked_steps(job.steps), p),
             Fact("skeleton", "not_interpreted", unread, Provenance("file", file.label))]
    if job.assumed:
        facts.append(Fact("skeleton", "assumed_values", job.assumed,
                          Provenance("assumed", "msdb's documented parameter defaults, where the script does not say")))
    return CatalogObject(f"agent_job:{job.name}", "agent_job", tuple(facts))


def _package(file: SourceFile, database: str,
             databases: dict[str, DatabaseContext]) -> tuple[CatalogObject, Callable[[Snapshot], list[Fact]]]:
    p = Provenance("file", file.label)
    identity = f"ssis_package:{file.label}"
    try:
        found = ssis.inventory(file.text or "")
    except ET.ParseError as failed:
        facts = (Fact("skeleton", "file", file.label, p), Fact("skeleton", "inventoried", False, p),
                 Fact("skeleton", "not_inventoried_reason", f"not well-formed XML: {failed}", p))
        unread = Fact("skeleton", "references_unavailable", [{"reason": f"not well-formed XML: {failed}"}], p)
        return CatalogObject(identity, "ssis_package", facts), lambda snapshot: [unread]
    facts = [Fact("skeleton", "file", file.label, p)]
    facts += [Fact("skeleton", name, value, p) for name, value in sorted(found.facts.items())]
    facts.append(Fact("skeleton", "secrets", found.marked, p))

    detail = Provenance("file", f"{file.label}, its SQL parsed by ScriptDom")

    def resolve(snapshot: Snapshot) -> list[Fact]:
        if not found.facts["inventoried"]:
            return [Fact("skeleton", "references", [], detail),
                    Fact("skeleton", "references_unavailable", [{"reason": found.facts["not_inventoried_reason"]}],
                         detail)]
        refs: list[dict] = []
        unavailable = ssis.unread_sql(found.facts)
        engagement = set(databases)

        def place(entry: dict) -> tuple[str, str | None]:
            """The database a connection names, or the folder's with the name it gives
            when that database is outside the extraction."""
            named = entry["database"]
            known = _canonical_database(named or "", databases)
            return (known or database), (named if named and known is None else None)

        def outside(ref: dict, elsewhere: str | None) -> dict:
            if elsewhere and ref["resolved"] is None and ref["external"] == "not in the catalog":
                ref["external"] = f"another database: the connection names {elsewhere}"
            return ref

        for entry in found.sql:
            db, elsewhere = place(entry)
            context = databases[db]
            where = {"task": entry["task"], **({"component": entry["component"]} if entry["component"] else {})}
            try:
                parsed = parser.parse(identity, _without_markers(entry["text"]), context.compatibility_level, True)
            except parser.ParseFailed as failed:
                unavailable.append({**where, "reason": f"the SQL did not parse: {failed}"})
                continue
            refs += [{**where, **outside(r, elsewhere)} for r in resolution.work_out(
                parsed, snapshot, resolution.context(db, "dbo", context.case_sensitive),
                system_types=set(context.system_types), schemas={s.casefold() for s in context.schemas},
                engagement=engagement)]
        for entry in found.tables:
            db, elsewhere = place(entry)
            parts = names.split_name(entry["name"])
            resolved, via, ambiguous = resolution.resolve(snapshot, parts, "object", resolution.context(
                db, "dbo", databases[db].case_sensitive))
            refs.append(outside({"task": entry["task"], "component": entry["component"], "name": list(parts),
                                 "namespace": "object", "line": None, "column": None, "resolved": resolved,
                                 "via": via, "external": None if resolved else f"ambiguous: {ambiguous}" if ambiguous
                                 else resolution.external(parts, engagement)}, elsewhere))
        return [Fact("skeleton", "references", refs, detail),
                Fact("skeleton", "references_unavailable", unavailable, detail)]
    return CatalogObject(identity, "ssis_package", tuple(facts)), resolve


def _without_markers(sql: str) -> str:
    """An SSIS statement's ? parameter markers as 0, outside literals, comments and quoted
    names, so the parser reads it; every position is unchanged."""
    out, i, n = list(sql), 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"[":
            close = {"'": "'", '"': '"', "[": "]"}[ch]
            j = i + 1
            while j < n and not (sql[j] == close and sql[j + 1:j + 2] != close):
                j += 2 if sql[j] == close else 1
            i = j + 1
        elif sql.startswith("--", i):
            i = sql.find("\n", i) if "\n" in sql[i:] else n
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            if ch == "?":
                out[i] = "0"
            i += 1
    return "".join(out)


def schemas_of(objects: list[CatalogObject], declared: dict[str, set[str]]) -> dict[str, frozenset[str]]:
    """Each folder database's schema names: those created, and those its objects are in."""
    out = {d: set(s) for d, s in declared.items()}
    for o in objects:
        if o.has_fact("name_parts"):
            database, schema, _ = o.fact("name_parts").value
            out.setdefault(database, set()).add(schema)
    return {d: frozenset(s) for d, s in out.items()}
