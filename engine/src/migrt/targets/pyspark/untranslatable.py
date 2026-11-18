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
CALL_RINGS = "#160"            # artefacts that call each other: imports inside the calling function
JOINED_WRITES = "#158"         # UPDATE and DELETE over joined rows, SET IDENTITY_INSERT
TRANSACTIONS_AND_ERRORS = "#119"   # transactions, XACT_ABORT, TRY/CATCH, messages
TEMP_TABLES_AND_GUARDS = "#120"    # temp tables, table variables, SELECT @v =, guarded statements
WRITE_FORMS = "#121"           # MERGE, OUTPUT, TOP in writes, INSERT ... EXEC, global temp tables
TRIGGERS = "#122"              # triggers fired by the write wrapper
CURSORS = "#124"               # faithful cursors
UNION_AND_VALUES = "#129"      # UNION, VALUES row sources, TOP over them
SCALAR_SUBQUERY = "#130"       # a query used as one value
EXISTS = "#131"                # EXISTS
CONVERSIONS = "#132"           # CAST and CONVERT
GOTO = "#133"                  # GOTO and labels
WINDOWS = "#140"               # window functions
CTES = "#141"                  # non-recursive common table expressions
WAITFOR = "#142"               # WAITFOR
LOOKUPS = "#143"               # role and catalog lookups
SERVER_PROPERTIES = "#144"     # SERVERPROPERTY
RAISING = "#64"                # raising a construct to an expression or a set-based step, and the Python UDF fallback
VALUE_TYPES = "#134"           # bit, string conversions, table-valued parameters, the other non-number types
LOWERING_ASSIST = "#63"        # lowering assist
NUMBER_HELPERS = "#87"         # the support library's number, date and time helpers
DOOMED_TRANSACTIONS = "#113"   # the library modelling doomed transactions, which reading XACT_STATE() depends on
STRING_HELPERS = "#47"         # the support library's comparison, collation and string helpers
# The ticket that builds the generation of each statement kind not generated yet: a construct of that kind is manual work for want
# of it. A kind not here waits for nothing: no ticket has taken it, which a manual item says by an empty `waits_for`.
TICKET_BY_KIND = {
    "begin_transaction": TRANSACTIONS_AND_ERRORS, "commit_transaction": TRANSACTIONS_AND_ERRORS,
    "rollback_transaction": TRANSACTIONS_AND_ERRORS, "save_point": TRANSACTIONS_AND_ERRORS,
    "rollback_to_save_point": TRANSACTIONS_AND_ERRORS, "transaction_depth": TRANSACTIONS_AND_ERRORS,
    "set_isolation_level": TRANSACTIONS_AND_ERRORS, "set_abort_on_error": TRANSACTIONS_AND_ERRORS,
    "try_catch": TRANSACTIONS_AND_ERRORS, "raise_error": TRANSACTIONS_AND_ERRORS, "rethrow": TRANSACTIONS_AND_ERRORS,
    "emit_message": TRANSACTIONS_AND_ERRORS, "error_field": TRANSACTIONS_AND_ERRORS,
    "create_temp_table": TEMP_TABLES_AND_GUARDS, "drop_temp_table": TEMP_TABLES_AND_GUARDS,
    "declare_row_set_variable": TEMP_TABLES_AND_GUARDS, "assign_from_rows": TEMP_TABLES_AND_GUARDS,
    "assign_from_single_value": TEMP_TABLES_AND_GUARDS,
    "merge_rows": WRITE_FORMS, "return_changed_rows": WRITE_FORMS, "create_global_temp_table": WRITE_FORMS,
    "read_changed_rows": TRIGGERS,
    "jump": GOTO, "declare_label": GOTO,
    "window": WINDOWS, "row_number": WINDOWS, "named_query": CTES, "wait": WAITFOR,
    "for_each_row": RAISING, "atomic_region": RAISING, "named_result": RAISING, "column_expression_function": RAISING,
    "role_membership": LOOKUPS, "catalog_lookup": LOOKUPS, "session_value": SERVER_PROPERTIES,
    "union": UNION_AND_VALUES, "read_values": UNION_AND_VALUES, "limit": UNION_AND_VALUES,
    "scalar_query": SCALAR_SUBQUERY, "exists": EXISTS,
    "convert": CONVERSIONS, "try_convert": CONVERSIONS, "convert_with_style": CONVERSIONS,
    "concat_null_propagating": STRING_HELPERS, "trim": STRING_HELPERS, "upper": STRING_HELPERS, "left_part": STRING_HELPERS,
    "date_add": NUMBER_HELPERS, "current_time": NUMBER_HELPERS, "date_diff_boundaries": NUMBER_HELPERS,
    "round": NUMBER_HELPERS, "decimal_divide": NUMBER_HELPERS, "last_identity": NUMBER_HELPERS,
    "next_sequence_value": NUMBER_HELPERS,
    "declare_cursor": CURSORS, "open_cursor": CURSORS, "fetch_cursor": CURSORS, "fetch_status": CURSORS,
    "close_cursor": CURSORS, "deallocate_cursor": CURSORS, "write_current_row": CURSORS,
}
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
            "and calls) is not built yet, so it is reported as manual work")


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
    if kind == "transaction_state":
        return ("reading the state of the transaction needs the support library to model a transaction that an error has "
                "doomed, which it does not yet, so it is manual work")
    if vocabulary.part_of(kind) == "procedural" or kind == "batch":
        return procedural_reason(kind)
    return f"{kind} has no PySpark generation yet"


def waits_for_kind(kind: str, handled: AbstractSet[str]) -> tuple[str, ...]:
    """What a node kind the target has no handler for waits for: procedural generation, or lowering assist. Dynamic
    SQL, schema and permission changes are manual work for good, and so are kinds nothing is planned for."""
    if kind == "opaque_fragment":
        return (LOWERING_ASSIST,)
    if kind == "transaction_state" and kind not in handled:
        return (DOOMED_TRANSACTIONS,)
    if kind not in handled and kind in TICKET_BY_KIND:
        return (TICKET_BY_KIND[kind],)
    if kind not in handled and (vocabulary.part_of(kind) == "procedural" or kind == "batch"):
        return ()
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
        elif node.kind == "external_reference" and node.attrs.get("global_temp_table"):
            # a read of a ##table the artefact never creates: never generated as if the table existed
            reason = f"{node.attrs['reason']}; {reason}"
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
