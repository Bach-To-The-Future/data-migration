"""The intermediate representation the core owns (spec section 4, ADR 0001).

A minimal first cut: the artefact's outer record and a tree of nodes, each an operation
or node kind from the operation vocabulary (Annex A) with its parameters as attributes,
a vocabulary type where it is a value, the source position it came from, and who made it.
Saved as a versioned JSON document per artefact; the format version is part of every key.
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any

FORMAT_VERSION = "ir-0.1"
DETERMINISTIC = "deterministic"


@dataclasses.dataclass(frozen=True)
class VType:
    """A vocabulary type, such as integer(bits=32, signed=True) or money(precision=19)."""
    name: str
    params: tuple[tuple[str, Any], ...] = ()

    @staticmethod
    def of(name: str, **params) -> "VType":
        return VType(name, tuple(sorted(params.items())))

    def param(self, key: str):
        return dict(self.params).get(key)

    def int_param(self, key: str) -> int:
        value = self.param(key)
        if not isinstance(value, int):
            raise ValueError(f"{self} has no integer parameter {key!r}")
        return value

    def to_json(self) -> dict:
        return {"name": self.name, **dict(self.params)}

    def __str__(self) -> str:
        inner = ", ".join(f"{k}={v}" for k, v in self.params)
        return f"{self.name}({inner})" if inner else self.name


UNKNOWN = VType.of("unknown")


@dataclasses.dataclass(frozen=True)
class SourceSpan:
    """Where a node came from in the artefact's original text: 1-based lines and columns,
    end inclusive-exclusive like ScriptDom, plus the character offset and length."""
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    offset: int
    length: int

    def to_json(self) -> dict:
        return dataclasses.asdict(self)

    def __str__(self) -> str:
        return f"L{self.start_line}:{self.start_column}-L{self.end_line}:{self.end_column}"


@dataclasses.dataclass
class Node:
    """One node. `kind` is a vocabulary name without its parameters (`join`, not
    `join(kind)`); parameters sit in `attrs`. `role` says what the node is to its parent."""
    kind: str
    span: SourceSpan | None
    type: VType | None = None
    attrs: dict[str, Any] = dataclasses.field(default_factory=dict)
    children: list["Node"] = dataclasses.field(default_factory=list)
    role: str = ""
    producer: str = DETERMINISTIC
    comments: list[str] = dataclasses.field(default_factory=list)
    id: str = ""

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    @property
    def value_type(self) -> VType:
        """The node's type, for a node the checker guarantees has one (every expression)."""
        if self.type is None:
            raise ValueError(f"{self.kind} node {self.id} has no type; the checker should have refused it")
        return self.type

    @property
    def position(self) -> SourceSpan:
        """The node's source position, which the checker guarantees every node has."""
        if self.span is None:
            raise ValueError(f"{self.kind} node {self.id} has no source position; the checker should have refused it")
        return self.span

    def children_with(self, role: str) -> list["Node"]:
        return [c for c in self.children if c.role == role]

    def child(self, role: str) -> "Node":
        [only] = self.children_with(role)
        return only

    def to_json(self) -> dict:
        out: dict[str, Any] = {"id": self.id, "kind": self.kind}
        if self.role:
            out["role"] = self.role
        if self.type is not None:
            out["type"] = self.type.to_json()
        if self.attrs:
            out["attrs"] = self.attrs
        out["span"] = self.span.to_json() if self.span else None
        out["producer"] = self.producer
        if self.comments:
            out["comments"] = self.comments
        if self.children:
            out["children"] = [c.to_json() for c in self.children]
        return out


@dataclasses.dataclass
class Artefact:
    """The one outer record every artefact has (spec section 4, item 9)."""
    identity: str
    artefact_type: str
    original_source: str
    body: Node
    dependencies: list[str] = dataclasses.field(default_factory=list)
    vocabulary_version: str = ""

    def number_nodes(self) -> None:
        """Give every node a stable id in tree order, so documents are deterministic."""
        for index, node in enumerate(self.body.walk(), start=1):
            node.id = f"n{index}"

    def nodes(self):
        return self.body.walk()

    def to_json(self) -> dict:
        return {
            "format_version": FORMAT_VERSION,
            "vocabulary_version": self.vocabulary_version,
            "identity": self.identity,
            "artefact_type": self.artefact_type,
            "dependencies": sorted(self.dependencies),
            "original_source": self.original_source,
            "body": self.body.to_json(),
        }

    def document(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True, ensure_ascii=False, indent=1)
