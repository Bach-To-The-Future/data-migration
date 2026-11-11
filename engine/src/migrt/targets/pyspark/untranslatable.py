"""Constructs the PySpark target cannot translate, detected before generation (spec section 5, "Generation").

Generation is never trusted to surface them: a translator that meets a form it does not know can drop it and say
nothing (a translator was seen dropping a clause of a merge statement with nothing logged). So the representation is
walked first, every node is checked against what this target has a handler for, and each finding is a node with a
reason. Generation then must account for every finding, as a manual item that replaces the node or one above it
(`generator.require_accounted`); a finding found nowhere in the file is an error, not a silent omission.

A finding is also made for a node that depends on a scope entry that may be stale (`scope_may_be_stale`, issue #43):
it is never generated as if its scope were certain.
"""
from __future__ import annotations

import dataclasses

from ...core import vocabulary
from ...core.ir import Artefact, Node

# The node kinds this target has a handler for. A handler may still decline a particular node with its own
# reason (a money product, a string compared by a collation): that is the generator's report, made at the node.
GENERATED_KINDS = frozenset({
    "view_body", "table_function_body", "scalar_function_body", "procedure_body", "trigger_body",
    "batch_script_body", "declare_parameter", "return",
    "read_table", "join", "filter", "project", "aggregate",
    "column", "literal", "variable", "null", "case", "implicit_convert",
    "add", "subtract", "multiply", "modulo", "integer_divide", "money_multiply", "money_divide",
    "count_values", "count_rows", "sum", "average", "minimum", "maximum",
    "null_replacement", "null_if_equal", "compare", "and", "or", "not", "is_null", "call_scalar_function",
})
PROCEDURAL_ISSUE = 50
SPECIAL = ("opaque_fragment", "dynamic_sql_execution", "schema_change", "permission_change", "external_reference")
STALE = "scope_may_be_stale"
UNSUPPORTED_TYPE = "unsupported_type"


@dataclasses.dataclass(frozen=True)
class Finding:
    node: str          # the node's id
    kind: str          # the node's kind, or the cause (`scope_may_be_stale`, `unsupported_type`)
    reason: str


def procedural_reason(kind: str) -> str:
    return (f"{kind} is procedural code: procedural generation (variables, control flow, temp tables, writes, "
            f"calls and session options) is built by issue #{PROCEDURAL_ISSUE}, until then it is reported as "
            "manual work")


def reason_for_kind(kind: str) -> str | None:
    """Why a node kind the target has no handler for is manual work. None for a kind it handles."""
    if kind in GENERATED_KINDS:
        return None
    if kind == "dynamic_sql_execution":
        return ("dynamic SQL is not generated: the statement is assembled at run time, so it is manual work (spec "
                "section 6)")
    if kind == "schema_change":
        return "a schema change inside a routine is not generated: it is manual work (spec section 4, item 6)"
    if kind == "permission_change":
        return "a permission change is not generated: it is manual work (spec section 4, item 6)"
    if kind == "opaque_fragment":
        return "kept as written by lowering, which could not express it: lowering assist (issue #63) is not built yet"
    if vocabulary.part_of(kind) == "procedural" or kind == "batch":
        return procedural_reason(kind)
    return f"{kind} has no PySpark generation yet"


def detect(artefact: Artefact) -> list[Finding]:
    """Every node the target cannot translate, in tree order."""
    found: list[Finding] = []
    for node in artefact.nodes():
        found += _findings(node)
    return found


def _findings(node: Node) -> list[Finding]:
    out: list[Finding] = []
    reason = reason_for_kind(node.kind)
    if reason is not None:
        if node.kind == "opaque_fragment":
            reason = f"{node.attrs.get('reason', 'lowering could not express it')}; {reason}"
        out.append(Finding(node.id, node.kind, reason))
    if node.type is not None and node.type.name == UNSUPPORTED_TYPE:
        out.append(Finding(node.id, UNSUPPORTED_TYPE,
                           f"its type {node.type.param('source_name')} has no neutral meaning, so anything touching "
                           "it is manual work (spec section 6)"))
    if node.kind == "implicit_convert" and node.attrs.get("fits"):
        out.append(Finding(node.id, node.kind,
                           "a store that cuts or rounds its value is reproduced by the support library's typed "
                           "assignment, which is not built yet"))
    if STALE in node.attrs:
        out.append(Finding(node.id, STALE,
                           f"the scope entries it depends on may be stale after the scope barrier at "
                           f"{node.attrs[STALE]}, so it is not generated as if its scope were certain"))
    return out
