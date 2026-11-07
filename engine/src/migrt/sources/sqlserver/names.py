"""SQL Server's name resolver: one-, two- and three-part names, the default schema, and
identifiers compared as the database's collation compares them. Synonyms are later work."""
from __future__ import annotations

from ...core.catalog import Snapshot


def identifiers_case_sensitive(collation: str) -> bool:
    return "_CS" in collation.upper() or collation.upper().endswith("_BIN") or collation.upper().endswith("_BIN2")


def resolve(snapshot: Snapshot, parts: tuple[str, ...], context: dict) -> str | None:
    """context: database, schemas (where a one-part name is looked for, in order),
    case_sensitive, kinds (the object kinds wanted).

    Inside a module SQL Server looks for a one-part name in the module's own schema, then
    in dbo (tested on the 2022 sandbox: a view in schema s read s.T, not dbo.T, for a
    caller whose default schema is dbo)."""
    if len(parts) == 1:
        candidates = [(context["database"], schema, parts[0]) for schema in context.get("schemas", ["dbo"])]
    elif len(parts) == 2:
        candidates = [(context["database"], *parts)]
    elif len(parts) == 3:
        candidates = [parts]
    else:
        return None  # four-part names go through a linked server: an external reference
    kinds = context.get("kinds")
    fold = (lambda s: s) if context.get("case_sensitive") else str.casefold
    for candidate in candidates:
        wanted = fold(".".join(candidate))
        matches = [o.identity for o in snapshot.objects
                   if fold(o.identity) == wanted and (kinds is None or o.kind in kinds)]
        if len(matches) > 1:
            raise AmbiguousName(".".join(parts), matches)
        if matches:
            return matches[0]
    return None


class AmbiguousName(Exception):
    """A name that matches more than one object, as names differing only by case do in a
    case-insensitive database. Reported, never resolved by guessing."""

    def __init__(self, name: str, matches: list[str]):
        self.matches = matches
        super().__init__(f"{name} is ambiguous: it matches {', '.join(matches)}")
