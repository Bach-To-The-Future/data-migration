"""The core's neutral error classes (ADR 0019; spec section 6, "Errors").

A target maps its runtime's failures to these classes and a source maps each class to its own codes, messages and
severities, so S sources and T targets need S + T mappings, not S x T. The list is data ([`error-classes.json`]); this
module reads it and offers what both halves are checked against. It imports nothing from a source or a target."""
from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import pathlib

PATH = pathlib.Path(__file__).with_name("error-classes.json")


class UnknownErrorClass(KeyError):
    """A mapping or a caller named an error class the core does not list."""


@dataclasses.dataclass(frozen=True)
class Detail:
    name: str
    meaning: str


@dataclasses.dataclass(frozen=True)
class ErrorClass:
    name: str
    meaning: str
    details: tuple[Detail, ...]

    @property
    def detail_names(self) -> frozenset[str]:
        return frozenset(d.name for d in self.details)


@functools.cache
def classes() -> dict[str, ErrorClass]:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    found: dict[str, ErrorClass] = {}
    for entry in data["classes"]:
        if entry["name"] in found:
            raise ValueError(f"error class {entry['name']} is listed twice")
        found[entry["name"]] = ErrorClass(entry["name"], entry["meaning"],
                                          tuple(Detail(d["name"], d["meaning"]) for d in entry["details"]))
    return found


def require(name: str) -> ErrorClass:
    try:
        return classes()[name]
    except KeyError:
        raise UnknownErrorClass(f"{name!r} is not an error class of the core; the list is {sorted(classes())}") from None


def version_of(listed: dict[str, ErrorClass]) -> str:
    """The version of a list: the hash of its content, so it changes exactly when the list does (ADR 0023)."""
    text = json.dumps([[c.name, c.meaning, [[d.name, d.meaning] for d in c.details]] for c in listed.values()],
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def version() -> str:
    return version_of(classes())
