"""The operation vocabulary (Annex A), read from the copy shipped with the core.

The spec's source of truth is docs/spec-drafts/operation-vocabulary.json; the engine's
tests check this copy is identical to it."""
from __future__ import annotations

import functools
import json
import pathlib

PATH = pathlib.Path(__file__).with_name("operation-vocabulary.json")


@functools.cache
def _data() -> dict:
    return json.loads(PATH.read_text(encoding="utf-8"))


def version() -> str:
    return _data()["version"]


def _bare(name: str) -> str:
    """'join(kind)' -> 'join'."""
    return name.split("(", 1)[0]


@functools.cache
def operations() -> frozenset[str]:
    return frozenset(_bare(op["name"]) for group in _data()["groups"] for op in group["operations"])


@functools.cache
def node_kinds() -> frozenset[str]:
    return frozenset(_bare(k["name"]) for k in _data()["node_kinds"])


@functools.cache
def types() -> frozenset[str]:
    return frozenset(_bare(t["name"]) for t in _data()["types"])


@functools.cache
def _parts() -> dict[str, str]:
    return {_bare(op["name"]): group["part"] for group in _data()["groups"] for op in group["operations"]}


def part_of(kind: str) -> str | None:
    """'procedural', 'query' or 'expression' for an operation; None for a node kind."""
    return _parts().get(kind)


def is_known(kind: str) -> bool:
    return kind in operations() or kind in node_kinds()
