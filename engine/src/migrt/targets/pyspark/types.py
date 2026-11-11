"""The PySpark half of the type mapping (spec section 6, ADR 0019): vocabulary types to Spark types, each with
the representation rule the write wrapper or a helper reproduces. The source's half is
`sources.sqlserver.types`; neither half speaks of the other's side.

One mapping, fixed, never configurable per client. The rules are published in every delivered project
(`representation_rules_markdown`), so the client reads what each type becomes and what is reproduced by code.
"""
from __future__ import annotations

import dataclasses
import hashlib

from ...core.ir import VType

# Vocabulary types that are not the type of a stored column: they have no Spark column type, by decision.
NOT_COLUMN_TYPES = frozenset({"collation", "row_set", "cursor_handle", "unsupported_type", "unknown"})
MAPPED_TYPES = frozenset({"boolean", "integer", "decimal", "money", "float", "date", "local_timestamp",
                          "timestamp_with_offset", "time_of_day", "string", "binary", "guid", "xml"})
ROW_VERSION_RULE = "Compared by property, never by raw value"


@dataclasses.dataclass(frozen=True)
class SparkType:
    ddl: str                          # the type as Spark SQL names it, in lowercase
    rule: str | None = None           # the representation rule, reproduced by the write wrapper or a helper
    flagged: bool = False             # the report flags every use
    companion_column: str | None = None   # a column the target table must also hold
    # Why a cast to this type is not exact by itself (a range, a padding or a length the support library
    # enforces), or None where `cast(...)` does it.
    inexact_cast: str | None = None


def _timestamp_rule(rounding: str) -> str:
    if rounding == "1/300 second":
        return "Rounding to 1/300 second, reproduced by the write wrapper"
    if rounding == "minute":
        return "Rounding to the minute, reproduced by the write wrapper"
    digits = int(rounding.split(" ", 1)[0])
    if digits > 6:
        return ("Keeps six fractional digits; the seventh is compared within a catalogued 100 ns tolerance, "
                "reported per artefact")
    return f"Rounding to {digits} fractional digits, reproduced by the write wrapper"


def mapping(t: VType) -> SparkType | None:
    """The Spark type of a vocabulary type, or None where the type has no Spark column type."""
    name = t.name
    if name == "boolean":
        return SparkType("boolean")
    if name == "integer":
        bits = t.int_param("bits")
        if not t.param("signed"):
            return SparkType("smallint", "Range 0-255 enforced",
                             inexact_cast="tinyint values are SMALLINT in Spark with their 0 to 255 range enforced "
                                          "by the support library, which is not built yet")
        return SparkType({16: "smallint", 32: "int", 64: "bigint"}[bits])
    if name == "decimal":
        return SparkType(f"decimal({t.param('precision')},{t.param('scale')})")
    if name == "money":
        return SparkType(f"decimal({t.param('precision')},4)", "Truncating arithmetic")
    if name == "float":
        return SparkType({53: "double", 24: "float"}[t.int_param("bits")])
    if name == "date":
        return SparkType("date")
    if name == "local_timestamp":
        rule = _timestamp_rule(t.str_param("rounding"))
        return SparkType("timestamp_ntz", rule, inexact_cast=f"{rule[0].lower()}{rule[1:]}")
    if name == "timestamp_with_offset":
        return SparkType("timestamp", "The offset is kept in a companion column, a target-table requirement listed "
                                      "in the report; seven fractional digits are kept as for a local timestamp",
                         companion_column="offset", inexact_cast="the offset lives in a companion column")
    if name == "time_of_day":
        return SparkType("string", "Fixed-width, zero-padded HH:MM:SS.fffffff, so text order is time order", True,
                         inexact_cast="a time is a fixed-width string")
    if name == "string":
        unit = "UTF-16 units" if t.param("unit") == "utf16_units" else "bytes"
        if t.length() == "unlimited":
            return SparkType("string", inexact_cast="a string is compared by its collation")
        padding = "Padded with spaces to the declared length; " if t.flag("fixed") else ""
        rule = f"{padding}{'l' if padding else 'L'}ength enforced with the source's truncation error, counted in {unit}"
        return SparkType("string", rule, inexact_cast="a string's length is enforced by the support library")
    if name == "binary":
        return SparkType("binary")
    if name == "guid":
        return SparkType("string", "Compared and ordered through the byte-order helper",
                         inexact_cast="a guid is compared by the source's byte order")
    if name == "xml":
        return SparkType("string", None, True, inexact_cast="xml is a string")
    return None


# The published rules: one row per kind of column, each worded by `mapping` itself, so the document and the
# generator cannot disagree. Rows are (the type as the document names it, a representative vocabulary type).
_STRING = {"unit": "bytes", "fixed": False, "collation": "any", "origin": "column"}
_REPRESENTATIVES = [
    ("boolean", VType.of("boolean")),
    ("integer(8, unsigned)", VType.of("integer", bits=8, signed=False)),
    ("integer(16)", VType.of("integer", bits=16, signed=True)),
    ("integer(32)", VType.of("integer", bits=32, signed=True)),
    ("integer(64)", VType.of("integer", bits=64, signed=True)),
    ("decimal(p,s), here decimal(18,2)", VType.of("decimal", precision=18, scale=2)),
    ("money(19)", VType.of("money", precision=19)),
    ("money(10)", VType.of("money", precision=10)),
    ("float(53)", VType.of("float", bits=53)),
    ("float(24)", VType.of("float", bits=24)),
    ("date", VType.of("date")),
    ("local_timestamp, rounding 1/300 second", VType.of("local_timestamp", rounding="1/300 second")),
    ("local_timestamp, rounding minute", VType.of("local_timestamp", rounding="minute")),
    ("local_timestamp, 7 digits", VType.of("local_timestamp", rounding="7 digits")),
    ("local_timestamp, 3 digits", VType.of("local_timestamp", rounding="3 digits")),
    ("timestamp_with_offset(7)", VType.of("timestamp_with_offset", digits=7)),
    ("time_of_day(7)", VType.of("time_of_day", digits=7)),
    ("string, varchar(n)", VType.of("string", length=20, **_STRING)),
    ("string, nvarchar(n)", VType.of("string", length=20, **{**_STRING, "unit": "utf16_units"})),
    ("string, char(n)", VType.of("string", length=20, **{**_STRING, "fixed": True})),
    ("string, unlimited", VType.of("string", length="unlimited", **_STRING)),
    ("binary", VType.of("binary", length=16, fixed=False)),
    ("guid", VType.of("guid")),
    ("xml", VType.of("xml")),
]


def version() -> str:
    """The version of this half of the type mapping, part of every cache key: the hash of its published rules,
    which are worded by the mapping itself. Until the support library carries it (issue #49), this is the version."""
    return hashlib.sha256(representation_rules_markdown().encode("utf-8")).hexdigest()


def representation_rules_markdown() -> str:
    """The document delivered in every project: what each vocabulary type becomes in Spark and which rule the
    write wrapper or a helper reproduces. Deterministic."""
    lines = ["# Representation rules", "",
             "How each type of the intermediate representation is held in Spark. Where a rule is stated, Spark's "
             "type alone does not keep the source's meaning: the support library's write wrapper or a helper "
             "reproduces it. A type that is not listed has no Spark column type, and anything touching it is "
             "manual work.", "",
             "| Vocabulary type | Spark type | Representation rule | Flagged |", "|---|---|---|---|"]
    for label, vocabulary_type in _REPRESENTATIVES:
        found = mapping(vocabulary_type)
        if found is None:
            raise ValueError(f"the representative type {label} has no Spark type")
        lines.append(f"| {label} | {found.ddl.upper()}{' plus a companion offset column' if found.companion_column else ''} "
                     f"| {found.rule or ''} | {'yes' if found.flagged else ''} |")
    lines.append(f"| binary(8, fixed), for row versions | BINARY | {ROW_VERSION_RULE} | |")
    return "\n".join(lines) + "\n"
