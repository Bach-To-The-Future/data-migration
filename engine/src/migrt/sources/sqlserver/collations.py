"""SQL Server's comparison rules (spec section 4: every string comparison names its rule).

A collation name says how two strings compare: case, accent, kana and width sensitivity, or
binary order. Which collation a comparison uses comes from collation precedence: an explicit
COLLATE clause wins; then the collation of a column or variable (implicit); then the database
default, which is all a literal or a converted value has (coercible-default). Two different
implicit collations in one comparison are a conflict, which SQL Server refuses at run time
(error 468); lowering keeps such an expression as an opaque fragment rather than choose.
"""
from __future__ import annotations

import dataclasses

EXPLICIT, COLUMN, DEFAULT = "explicit", "column", "default"
STRENGTH = {EXPLICIT: 3, COLUMN: 2, DEFAULT: 1}


class CollationConflict(Exception):
    def __init__(self, first: "Label", second: "Label"):
        self.first, self.second = first, second
        super().__init__(f"collation conflict between {first.collation} ({first.origin}) and "
                         f"{second.collation} ({second.origin}): SQL Server refuses to compare them")


@dataclasses.dataclass(frozen=True)
class Label:
    """A string's collation and where it came from (explicit, column or default)."""
    collation: str
    origin: str


def combine(first: Label, second: Label) -> Label:
    """The collation label of an expression over two strings, by SQL Server's precedence."""
    if first.collation == second.collation:
        return first if STRENGTH[first.origin] >= STRENGTH[second.origin] else second
    if STRENGTH[first.origin] == STRENGTH[second.origin]:
        raise CollationConflict(first, second)
    return first if STRENGTH[first.origin] > STRENGTH[second.origin] else second


def rule(label: Label) -> dict:
    """The comparison rule a collation gives, as the intermediate representation records it.
    SQL Server pads the shorter string with spaces in every comparison, so trailing spaces
    never count; `collation` keeps the name, for whatever the booleans do not say (code
    page, binary order, version)."""
    parts = label.collation.upper().split("_")
    binary = any(p in ("BIN", "BIN2") for p in parts)
    return {"collation": label.collation, "origin": label.origin,
            "case_sensitive": binary or "CS" in parts, "accent_sensitive": binary or "AS" in parts,
            "kana_sensitive": binary or "KS" in parts, "width_sensitive": binary or "WS" in parts,
            "binary": binary, "ignore_trailing_spaces": True}
