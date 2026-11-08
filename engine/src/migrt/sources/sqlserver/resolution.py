"""Resolving the names the parser found (references.find) against the whole catalog, the
same way for every input: a module read from a server, one parsed from a file, a query
batch, a SQL Agent job step or the SQL embedded in an SSIS package.

Each reference keeps the name as written, where it is, what it resolves to, the synonym it
was named through if any, and, when it resolves to nothing, why: an external reference, or
a name that is ambiguous, which is never taken for a missing one.
"""
from __future__ import annotations

from ...core.catalog import Snapshot
from . import names, references
from .parser import Parsed


def context(database: str, schema: str, case_sensitive: bool, kinds=None) -> dict:
    """Where a name written in `schema` of `database` is looked for: that schema, then dbo."""
    return {"database": database, "schemas": [schema, "dbo"] if schema != "dbo" else ["dbo"],
            "case_sensitive": case_sensitive, "kinds": kinds}


def resolve(snapshot: Snapshot, parts: tuple[str, ...], namespace: str,
            where: dict) -> tuple[str | None, str | None, str | None]:
    """What a name denotes, the synonym it was named through if any, and, when it denotes
    nothing because it is ambiguous, why."""
    kinds = names.TYPE_KINDS if namespace == "type" else None
    try:
        found = names.resolve(snapshot, parts, {**where, "kinds": kinds})
    except names.AmbiguousName as ambiguous:
        return None, None, str(ambiguous)
    via = next((identity for identity, fact in found.consulted if fact == "base_object"), None)
    return found.identity, via, None


def external(parts: tuple[str, ...], engagement: set[str]) -> str:
    """Why a name that resolves to nothing is outside the catalog."""
    if len(parts) == 4:
        return "linked server"
    if len(parts) == 3 and parts[0].casefold() not in {d.casefold() for d in engagement}:
        return "another database"
    if (len(parts) >= 2 and parts[-2].casefold() in ("sys", "information_schema")) or (
            len(parts) == 1 and parts[0].casefold().startswith(("sp_", "xp_", "fn_"))):
        return "system object"
    return "not in the catalog"


def work_out(parsed: Parsed, snapshot: Snapshot, where: dict, *, system_types: set[str], schemas: set[str],
             engagement: set[str]) -> list[dict]:
    """Every reference in one parsed text, resolved."""
    found = []
    for ref in references.find(parsed, system_types, schemas):
        resolved, via, ambiguous = ((None, None, None) if ref.external else
                                    resolve(snapshot, ref.parts, ref.namespace, where))
        why = ref.external or (None if resolved else f"ambiguous: {ambiguous}" if ambiguous else
                               external(ref.parts, engagement))
        found.append({**ref.to_json(), "resolved": resolved, "via": via, "external": why})
    return found
