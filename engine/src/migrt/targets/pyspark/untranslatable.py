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
from collections.abc import Set as AbstractSet

from ...core import vocabulary
from ...core.ir import Artefact, Node

# What a construct waits for, as `waits_for` names it (never in a reason, which a client may read).
PROCEDURAL = "#50"             # procedural generation
LOWERING_ASSIST = "#63"        # lowering assist
NUMBER_HELPERS = "#87"         # the support library's number, date and time helpers
STRING_HELPERS = "#47"         # the support library's comparison, collation and string helpers
SPECIAL = ("opaque_fragment", "dynamic_sql_execution", "schema_change", "permission_change", "external_reference")
STALE = "scope_may_be_stale"
UNSUPPORTED_TYPE = "unsupported_type"


@dataclasses.dataclass(frozen=True)
class Finding:
    node: str          # the node's id
    kind: str          # the node's kind, or the cause (`scope_may_be_stale`, `unsupported_type`)
    reason: str
    waits_for: tuple[str, ...] = ()    # what it is manual work for want of, if anything (see `Unsupported`)


def procedural_reason(kind: str) -> str:
    return (f"{kind} is procedural code: procedural generation (variables, control flow, temp tables, writes, "
            "calls and session options) is not built yet, so it is reported as manual work")


def reason_for_kind(kind: str, handled: AbstractSet[str]) -> str | None:
    """Why a node kind the target has no handler for is manual work. None for a kind it handles. `handled` is the
    generator's own answer to which kinds those are (`generator.handled_kinds()`): there is no list of them here."""
    if kind in handled:
        return None
    if kind == "dynamic_sql_execution":
        return ("dynamic SQL is not generated: the statement is assembled at run time, so it is manual work (spec "
                "section 6)")
    if kind == "schema_change":
        return "a schema change inside a routine is not generated: it is manual work (spec section 4, item 6)"
    if kind == "permission_change":
        return "a permission change is not generated: it is manual work (spec section 4, item 6)"
    if kind == "opaque_fragment":
        return "kept as written by lowering, which could not express it: lowering assist is not built yet"
    if vocabulary.part_of(kind) == "procedural" or kind == "batch":
        return procedural_reason(kind)
    return f"{kind} has no PySpark generation yet"


def waits_for_kind(kind: str, handled: AbstractSet[str]) -> tuple[str, ...]:
    """What a node kind the target has no handler for waits for: procedural generation, or lowering assist. Dynamic
    SQL, schema and permission changes are manual work for good, and so are kinds nothing is planned for."""
    if kind == "opaque_fragment":
        return (LOWERING_ASSIST,)
    if kind not in handled and (vocabulary.part_of(kind) == "procedural" or kind == "batch"):
        return (PROCEDURAL,)
    return ()


def detect(artefact: Artefact, handled: AbstractSet[str]) -> list[Finding]:
    """Every node the target cannot translate, in tree order: those of a kind it has no handler for (`handled`), and
    those it has one for but must not generate as they are."""
    found: list[Finding] = []
    for node in artefact.nodes():
        found += _findings(node, handled)
    return found


def _findings(node: Node, handled: AbstractSet[str]) -> list[Finding]:
    out: list[Finding] = []
    reason = reason_for_kind(node.kind, handled)
    if reason is not None:
        if node.kind == "opaque_fragment":
            reason = f"{node.attrs.get('reason', 'lowering could not express it')}; {reason}"
        out.append(Finding(node.id, node.kind, reason, waits_for_kind(node.kind, handled)))
    if node.type is not None and node.type.name == UNSUPPORTED_TYPE:
        out.append(Finding(node.id, UNSUPPORTED_TYPE,
                           f"its type {node.type.param('source_name')} has no neutral meaning, so anything touching "
                           "it is manual work (spec section 6)"))
    if node.kind == "implicit_convert" and node.attrs.get("fits"):
        # A string is cut or padded to its length, a decimal rounded to its scale: each type's own helpers do it.
        helpers = STRING_HELPERS if node.type is not None and node.type.name == "string" else NUMBER_HELPERS
        out.append(Finding(node.id, node.kind,
                           "a store that cuts or rounds its value is reproduced by the support library's typed "
                           "assignment, which is not built yet", (helpers,)))
    if STALE in node.attrs:
        out.append(Finding(node.id, STALE,
                           f"the scope entries it depends on may be stale after the scope barrier at "
                           f"{node.attrs[STALE]}, so it is not generated as if its scope were certain"))
    return out
