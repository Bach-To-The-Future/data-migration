"""The SQL Server half of the error mapping (ADR 0019; spec section 6, "Errors"): the core's neutral error classes to SQL
Server's error numbers, messages and severities, as data ([`error-mapping.json`]).

The engine reads it to check it, to flag a comparison against a number it does not cover, and to put it into every delivered
project, where the support library applies it at run time. Rendering is the library's, so there is one interpreter."""
from __future__ import annotations

import functools
import hashlib
import json
import pathlib

PATH = pathlib.Path(__file__).with_name("error-mapping.json")

# The numbers a program may raise itself (THROW takes 50000 and above; RAISERROR of a text is 50000). A comparison against
# one of them is about the program's own errors, which the generated code raises with the number the program gave.
USER_NUMBERS_FROM = 50000


@functools.cache
def _loaded() -> str:
    return PATH.read_text(encoding="utf-8")


def load() -> dict:
    return json.loads(_loaded())


def version() -> str:
    """The hash of the content, not of the file's bytes, so a checkout with other line endings has the same version (ADR 0023)."""
    canonical = json.dumps(load(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def mapped_numbers() -> set[int]:
    """Every number the mapping can give, not counting the ones the program supplies itself."""
    found: set[int] = set()
    for entry in load()["classes"].values():
        for part in entry.get("variants", [entry]):
            if isinstance(part["number"], int):
                found.add(part["number"])
    return found


def reserved_number() -> int:
    return int(load()["reserved"]["number"])


def covers(number: int) -> bool:
    """Whether a comparison of an error number against `number` is about an error the mapping reproduces. Zero (no error),
    the numbers the mapping gives and the numbers a program raises itself are covered. Anything else, such as a deadlock or
    an error in the system's own range with no class, is not: the generated code cannot raise it, and the comparison is
    flagged."""
    return number == 0 or number in mapped_numbers() or number >= USER_NUMBERS_FROM
