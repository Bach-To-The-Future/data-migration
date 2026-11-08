"""References worked out by the parser (spec section 3, "References"): the names a module's
text uses for other objects, as written, with where they are. They are the source of truth;
SQL Server's own dependency metadata is only a cross-check (extractor.cross_check).

Found: every schema object name ScriptDom parses (tables, views and synonyms read or
written, procedures executed, table-valued functions, sequences, TRUNCATE and other
statements' targets), user-defined types, schema-qualified scalar function calls, and
OPENQUERY, OPENROWSET and OPENDATASOURCE as external references. Not references: the
module's own name, a trigger's table (held as the trigger's `table` fact), names the module
creates (SELECT INTO, CREATE TABLE), temp tables, table variables, common table expressions,
aliases, a trigger's `inserted` and `deleted`, system data types, and methods called on a
column, such as XML's nodes().
"""
from __future__ import annotations

import dataclasses

from .parser import Parsed, walk

OWN_NAME = {"CreateViewStatement": "SchemaObjectName", "AlterViewStatement": "SchemaObjectName",
            "CreateOrAlterViewStatement": "SchemaObjectName", "CreateFunctionStatement": "Name",
            "AlterFunctionStatement": "Name", "CreateOrAlterFunctionStatement": "Name",
            "CreateTriggerStatement": "Name", "AlterTriggerStatement": "Name", "CreateOrAlterTriggerStatement": "Name",
            "CreateTableStatement": "SchemaObjectName"}
PROCEDURES = {"CreateProcedureStatement", "AlterProcedureStatement", "CreateOrAlterProcedureStatement"}
TRIGGERS = {"CreateTriggerStatement", "AlterTriggerStatement", "CreateOrAlterTriggerStatement"}
PSEUDO_TABLES = {"inserted", "deleted"}


@dataclasses.dataclass(frozen=True)
class Found:
    parts: tuple[str, ...]      # as written; an omitted middle part ("db..t") is ""
    namespace: str              # "object" or "type"
    line: int
    column: int
    external: str | None = None  # set when the syntax itself says the target is outside the catalog

    def to_json(self) -> dict:
        return {"name": list(self.parts), "namespace": self.namespace, "line": self.line, "column": self.column}


def _name(fragment) -> str:
    return str(fragment.GetType().Name)


def _value(identifier) -> str | None:
    return None if identifier is None else str(identifier.Value)


def _parts(schema_object_name) -> tuple[str, ...]:
    raw = [_value(schema_object_name.ServerIdentifier), _value(schema_object_name.DatabaseIdentifier),
           _value(schema_object_name.SchemaIdentifier), _value(schema_object_name.BaseIdentifier)]
    while raw and raw[0] is None:
        raw.pop(0)
    return tuple(p or "" for p in raw)


def _key(fragment) -> tuple[int, int]:
    return int(fragment.StartOffset), int(fragment.FragmentLength)


def find(parsed: Parsed, system_types: set[str], schemas: set[str]) -> list[Found]:
    """The references in one parsed module. `system_types` are the server's built-in type
    names and `schemas` the database's schema names, both folded to lower case."""
    names: list = []                          # (SchemaObjectName fragment, namespace)
    excluded: set[tuple[int, int]] = set()
    types: set[tuple[int, int]] = set()      # names of user-defined types
    local: set[str] = set()                   # CTE names and aliases, folded
    calls: list = []                          # FunctionCall fragments with a call target
    in_from: set[tuple[int, int]] = set()     # names of table-valued function calls in FROM
    external: list[Found] = []
    in_trigger = False

    def visit(f) -> None:
        nonlocal in_trigger
        kind = _name(f)
        if kind == "SchemaObjectName" or kind == "ChildObjectName":
            names.append(f)
        if kind in OWN_NAME:
            own = getattr(f, OWN_NAME[kind])
            if own is not None:
                excluded.add(_key(own))
        if kind in PROCEDURES and f.ProcedureReference is not None:
            excluded.add(_key(f.ProcedureReference.Name))
        if kind in TRIGGERS:
            in_trigger = True
            if f.TriggerObject is not None and f.TriggerObject.Name is not None:
                excluded.add(_key(f.TriggerObject.Name))
        if kind in ("SqlDataTypeReference", "XmlDataTypeReference") and f.Name is not None:
            excluded.add(_key(f.Name))   # a built-in type; an XML schema collection is its own name
        if kind == "SchemaObjectFunctionTableReference":
            in_from.add(_key(f.SchemaObject))
        if kind == "UserDataTypeReference" and f.Name is not None:
            types.add(_key(f.Name))
        if kind == "SelectStatement" and f.Into is not None:
            excluded.add(_key(f.Into))
        if kind == "CommonTableExpression":
            local.add(str(f.ExpressionName.Value).casefold())
        if kind.endswith("TableReference") or kind == "QueryDerivedTable":
            alias = getattr(f, "Alias", None)
            if alias is not None:
                local.add(str(alias.Value).casefold())
        if kind == "FunctionCall" and f.CallTarget is not None and _name(f.CallTarget) == "MultiPartIdentifierCallTarget":
            calls.append(f)
        if kind == "OpenQueryTableReference":
            external.append(Found((_value(f.LinkedServer) or "",), "object", int(f.StartLine), int(f.StartColumn),
                                  "linked server (OPENQUERY)"))
        if kind in ("OpenRowsetTableReference", "AdHocTableReference", "BulkOpenRowset"):
            external.append(Found(({"AdHocTableReference": "OPENDATASOURCE"}.get(kind, "OPENROWSET"),), "object",
                                  int(f.StartLine), int(f.StartColumn),
                                  "ad hoc connection (OPENROWSET or OPENDATASOURCE)"))

    walk(parsed.tree, visit)

    found: list[Found] = []
    for f in names:
        key = _key(f)
        if key in excluded or f.BaseIdentifier is None:
            continue
        parts = _parts(f)
        base = parts[-1]
        if base.startswith("#"):
            continue
        namespace = "type" if key in types else "object"
        if len(parts) == 1 and namespace == "object":
            if base.casefold() in local or (in_trigger and base.casefold() in PSEUDO_TABLES):
                continue
        if key in in_from and len(parts) >= 2 and (parts[0].casefold() in local or
                                                   (len(parts) == 2 and parts[0].casefold() not in schemas)):
            continue   # a method on a column, such as alias.Resume.nodes(...) or Instructions.nodes(...)
        schema = parts[-2] if len(parts) >= 2 else "sys"
        if namespace == "type" and schema.casefold() == "sys" and base.casefold() in system_types:
            continue
        found.append(Found(parts, namespace, int(f.StartLine), int(f.StartColumn)))
    for call in calls:
        target = [str(i.Value) for i in call.CallTarget.MultiPartIdentifier.Identifiers]
        if len(target) == 1 and (target[0].casefold() in local or target[0].casefold() not in schemas):
            continue   # a method call on a column or alias, such as Location.STDistance(...)
        found.append(Found((*target, str(call.FunctionName.Value)), "object", int(call.StartLine), int(call.StartColumn)))
    found += external
    return sorted(set(found), key=lambda r: (r.line, r.column, r.parts))
