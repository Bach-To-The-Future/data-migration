"""The client administrator's export of procedure result shapes, read as a folder input
(spec section 3, Annex F).

The extractor's login never holds EXECUTE, so SQL Server will not describe a procedure's
first result set to it. A client who wants shapes without the sandbox has their administrator
run the script in Annex F under their own account and save the result as CSV. This module
reads that file: one row per column of a procedure's first result set, one row for a
procedure with no result set, and one row, with the server's error, for a procedure SQL
Server could not describe. Nothing here touches a server.
"""
from __future__ import annotations

import csv
import dataclasses
import io
import re

# The script's columns, in its order. A file is an export when its header holds all of them.
COLUMNS = ("schema_name", "procedure_name", "column_ordinal", "column_name", "system_type_name",
           "user_type_schema", "user_type_name", "max_length", "precision", "scale", "is_nullable",
           "error_number", "error_message")
_BASE_TYPE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)")


def base_type(system_type_name: str) -> str | None:
    """The type's name, without its length, precision and scale: "nvarchar(50)" is "nvarchar"."""
    found = _BASE_TYPE.match(system_type_name)
    return found.group(1).casefold() if found else None


class ExportError(Exception):
    """A row of an export that cannot be read; the message names its line."""


@dataclasses.dataclass(frozen=True)
class Entry:
    """What the administrator's server said of one procedure: its shape, or why it could not
    be described. Exactly one of the two is set; a procedure with no result set has an empty shape."""
    schema: str
    procedure: str
    shape: list[dict] | None
    error: str | None


def _header(text: str) -> list[str] | None:
    try:
        row = next(csv.reader(io.StringIO(text)), None)
    except csv.Error:
        return None
    return [c.strip().casefold() for c in row] if row else None


def is_export(text: str) -> bool:
    """Whether the file's header is the script's."""
    header = _header(text)
    return header is not None and set(COLUMNS) <= set(header)


def _whole(row: dict, field: str, line: int) -> int | None:
    """A whole number, or empty for NULL. A bit may be written 0 or 1, as SSMS does, or
    False or True, as Azure Data Studio does."""
    value = row[field].strip()
    if value == "":
        return None
    if field == "is_nullable" and value.casefold() in ("true", "false"):
        return int(value.casefold() == "true")
    try:
        return int(value)
    except ValueError:
        raise ExportError(f"line {line}: {field} is not a whole number: {value!r}") from None


def _column(row: dict, line: int) -> dict:
    system = row["system_type_name"].strip()
    base_name = base_type(system)
    if base_name is None:
        raise ExportError(f"line {line}: system_type_name is not a type: {system!r}")
    fields = {f: _whole(row, f, line) for f in ("max_length", "precision", "scale", "is_nullable")}
    missing = [f for f, v in fields.items() if v is None]
    if missing:
        raise ExportError(f"line {line}: a column row needs {', '.join(missing)}")
    return {"name": row["column_name"] if row["column_name"] != "" else None,
            "declared_type": row["user_type_name"].strip() or base_name, "system_type": base_name,
            "max_length": fields["max_length"], "precision": fields["precision"], "scale": fields["scale"],
            "nullable": bool(fields["is_nullable"])}


def parse(text: str) -> list[Entry]:
    """The export's entries, in file order. Raises ExportError naming the line of a row that
    is not what the script writes."""
    reader = csv.DictReader(io.StringIO(text))
    reader.fieldnames = [f.strip().casefold() for f in reader.fieldnames or []]
    entries: dict[tuple[str, str], tuple[list[dict], str | None]] = {}
    while True:
        line = reader.line_num + 1
        try:
            row = next(reader, None)
        except csv.Error as failed:
            raise ExportError(f"line {line}: {failed}") from None
        if row is None:
            break
        if any(row.get(c) is None for c in COLUMNS):
            raise ExportError(f"line {line}: the row has fewer fields than the header")
        key = (row["schema_name"], row["procedure_name"])
        if key[0] == "" or key[1] == "":
            raise ExportError(f"line {line}: a row needs schema_name and procedure_name")
        columns, error = entries.setdefault(key, ([], None))
        number = _whole(row, "error_number", line)
        if number is not None:
            # SQL Server may give several error rows for one procedure; the first is the reason.
            if columns:
                raise ExportError(f"line {line}: {key[0]}.{key[1]} has an error row beside column rows")
            if error is None:
                entries[key] = ([], f"SQL Server could not describe it (error {number}): "
                                    f"{row['error_message'].strip()}")
        elif _whole(row, "column_ordinal", line) is not None:
            if error is not None:
                raise ExportError(f"line {line}: {key[0]}.{key[1]} has a column beside an error row")
            columns.append(_column(row, line))
    return [Entry(schema, procedure, None if error else columns, error)
            for (schema, procedure), (columns, error) in entries.items()]
