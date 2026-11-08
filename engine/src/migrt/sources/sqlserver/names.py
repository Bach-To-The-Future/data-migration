"""SQL Server's name resolver (spec section 3): one-, two- and three-part names, the
default schema, identifiers compared as the database's collation compares them, a separate
namespace for types, and synonyms."""
from __future__ import annotations

from ...core.catalog import Resolution, Snapshot

# Kinds named in a schema's object namespace, and in its type namespace. Everything else
# (the server, databases, raw captures, jobs, database triggers) is never named this way.
OBJECT_KINDS = {"table", "view", "procedure", "scalar_function", "inline_function", "table_function",
                "clr_procedure", "clr_scalar_function", "clr_table_function", "clr_aggregate", "trigger",
                "clr_trigger", "synonym", "sequence", "other_object"}
TYPE_KINDS = {"user_type", "table_type"}
TYPE_PREFIX = "type:"


def identifiers_case_sensitive(collation: str) -> bool:
    return "_CS" in collation.upper() or collation.upper().endswith("_BIN") or collation.upper().endswith("_BIN2")


def type_identity(database: str, schema: str, name: str) -> str:
    return f"{TYPE_PREFIX}{database}.{schema}.{name}"


def _index(snapshot: Snapshot, case_sensitive: bool) -> dict[str, list[str]]:
    fold = (lambda s: s) if case_sensitive else str.casefold
    index: dict[str, list[str]] = {}
    for o in snapshot.objects:
        if o.kind in OBJECT_KINDS or o.kind in TYPE_KINDS:
            index.setdefault(fold(o.identity), []).append(o.identity)
    return index


def resolve(snapshot: Snapshot, parts: tuple[str, ...], context: dict) -> Resolution:
    """context: database, schemas (where a one-part name is looked for, in order),
    case_sensitive, kinds (the object kinds wanted; types are looked for only when asked).

    Inside a module SQL Server looks for a one-part name in the module's own schema, then
    in dbo (tested on the 2022 sandbox: a view in schema s read s.T, not dbo.T, for a
    caller whose default schema is dbo). A synonym resolves to its base object when that is
    in the catalog, and otherwise to the synonym itself; SQL Server never follows a synonym
    to another synonym, and neither does this."""
    if len(parts) == 1:
        candidates = [(context["database"], schema, parts[0]) for schema in context.get("schemas", ["dbo"])]
    elif len(parts) == 2:
        candidates = [(context["database"], *parts)]
    elif len(parts) == 3 and parts[1] == "":
        # db..name: the omitted schema is looked for as for a one-part name.
        candidates = [(parts[0] or context["database"], schema, parts[2]) for schema in context.get("schemas", ["dbo"])]
    elif len(parts) == 3:
        candidates = [(parts[0] or context["database"], parts[1], parts[2])]
    else:
        return Resolution(None)  # four-part names go through a linked server: an external reference
    kinds = context.get("kinds")
    types = kinds is not None and bool(set(kinds) & TYPE_KINDS)
    case_sensitive = bool(context.get("case_sensitive"))
    fold = (lambda s: s) if case_sensitive else str.casefold
    index = snapshot.memo(("sqlserver-names", case_sensitive), lambda: _index(snapshot, case_sensitive))
    for candidate in candidates:
        wanted = fold((TYPE_PREFIX if types else "") + ".".join(candidate))
        matches = index.get(wanted, [])
        if len(matches) > 1:
            raise AmbiguousName(".".join(parts), matches)
        if not matches:
            continue
        found = snapshot.get(matches[0])
        if found.kind == "synonym":
            base = tuple(found.fact("base_object").value)
            # The base name is bound where the synonym is: its database, then dbo.
            where = {**context, "database": found.identity.split(".")[0], "schemas": ["dbo"], "kinds": None}
            target = resolve(snapshot, base, where) if len(base) <= 3 else Resolution(None)
            consulted = ((found.identity, "base_object"),)
            if target.identity is not None and snapshot.get(target.identity).kind != "synonym":
                if kinds is None or snapshot.get(target.identity).kind in kinds:
                    return Resolution(target.identity, consulted + target.consulted)
                continue
            return Resolution(found.identity, consulted)
        if kinds is None or found.kind in kinds:
            return Resolution(found.identity)
    return Resolution(None)


class AmbiguousName(Exception):
    """A name that matches more than one object, as names differing only by case do in a
    case-insensitive database. Reported, never resolved by guessing."""

    def __init__(self, name: str, matches: list[str]):
        self.matches = matches
        super().__init__(f"{name} is ambiguous: it matches {', '.join(matches)}")
