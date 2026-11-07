"""The checker (spec section 4, item 8): runs after every step that creates or changes the
intermediate representation, whoever made the change.

It verifies that every node is in the vocabulary and has a source position, that every
name resolves to the catalog or to something in scope, that types agree, and that every
opaque fragment is accounted for. First cut: the checks the view skeleton needs.
"""
from __future__ import annotations

import dataclasses
from typing import Callable

from . import vocabulary
from .ir import Artefact, Node

# Operations whose operands must be of one type family.
INTEGER_OPERANDS = {"integer_divide"}
MONEY_OPERAND = {"money_multiply", "money_divide"}
VALUE_KINDS = {"column", "literal", "null"}


@dataclasses.dataclass(frozen=True)
class Finding:
    node_id: str
    message: str


class CheckFailed(Exception):
    def __init__(self, identity: str, findings: list[Finding]):
        self.findings = findings
        super().__init__(f"{identity} failed the checker: " + "; ".join(f"{f.node_id}: {f.message}" for f in findings))


def check(artefact: Artefact, columns_of: Callable[[str], set[str]]) -> list[Finding]:
    """columns_of(table identity) gives the column names the catalog holds for it."""
    findings: list[Finding] = []

    def fail(node: Node, message: str) -> None:
        findings.append(Finding(node.id, message))

    aliases = {n.attrs["alias"]: n.attrs["table"] for n in artefact.nodes() if n.kind == "read_table"}
    for node in artefact.nodes():
        if not vocabulary.is_known(node.kind):
            fail(node, f"{node.kind} is not in the operation vocabulary")
        if node.span is None:
            fail(node, "has no source position")
        if node.kind == "opaque_fragment":
            if not node.attrs.get("reason") or not node.attrs.get("text"):
                fail(node, "an opaque fragment needs its reason and its verbatim text")
            continue
        if node.kind == "read_table" and node.attrs["table"] not in artefact.dependencies:
            fail(node, f"reads {node.attrs['table']}, which is not among the dependencies")
        if node.kind == "column":
            table = aliases.get(node.attrs["relation"])
            if table is None or table != node.attrs["table"]:
                fail(node, f"relation {node.attrs['relation']} is not in scope as {node.attrs['table']}")
            elif node.attrs["column"] not in columns_of(table):
                fail(node, f"{table} has no column {node.attrs['column']}")
        if vocabulary.part_of(node.kind) == "expression" or node.kind in VALUE_KINDS:
            if node.type is None or node.type.name == "unknown":
                fail(node, "an expression without an established type")
        operand_types = [c.type.name for c in node.children_with("argument") if c.type is not None]
        if node.kind in INTEGER_OPERANDS and operand_types != ["integer", "integer"]:
            fail(node, f"{node.kind} needs two integer operands, not {operand_types}")
        if node.kind in MONEY_OPERAND and "money" not in operand_types:
            fail(node, f"{node.kind} needs a money operand, not {operand_types}")
    outputs = [n.attrs["output_name"] for n in artefact.nodes() if n.role == "output"]
    if len(outputs) != len({o.casefold() for o in outputs}):
        findings.append(Finding(artefact.body.id, f"output names are not unique: {outputs}"))
    return findings


def require(artefact: Artefact, columns_of: Callable[[str], set[str]]) -> None:
    findings = check(artefact, columns_of)
    if findings:
        raise CheckFailed(artefact.identity, findings)
