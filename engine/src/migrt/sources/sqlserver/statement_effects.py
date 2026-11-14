"""What each statement leaves in `@@ROWCOUNT` and `@@ERROR` (spec section 4, operations `rows_affected` and
`last_statement_error`): "every statement resets it", with the exceptions SQL Server makes.

`rows_affected` and `last_statement_error` lower to reads of the session's last values, so a faithful program has to
set them as SQL Server's statements do. This is the source's half of that meaning, as data: for every kind of
statement the representation has, what it leaves in each. Every entry was decided against SQL Server in the sandbox
(`tests/test_statement_effects_sqlserver.py` runs each one and compares), not read from the documentation, which is
incomplete on this: `DECLARE @c CURSOR` keeps `@@ROWCOUNT` and resets `@@ERROR`, and `BREAK` keeps both.

`rows` is what `@@ROWCOUNT` holds after the statement: `keeps` the value from before it, `rows` the number of rows it
read or changed, `one` or `zero`, `fetch` one if the fetch found a row and zero if it did not, `callee` whatever the
last statement of the called procedure left, `unknown` where the statement kept as written could be anything.
`error` is what `@@ERROR` holds: `keeps`, `reset` (zero if the statement succeeded, its error's number if it failed),
or `unknown`.

A statement that holds others (`if`, `while`, `try_catch`) is described by what it does itself: evaluating the
condition of an `if` or a `while`, or reaching the end of a `TRY` block that raised nothing. The statements inside
have their own effects, run in their own turn."""
from __future__ import annotations

import dataclasses

from ...core.ir import Node


@dataclasses.dataclass(frozen=True)
class Effect:
    rows: str
    error: str


KEEPS = Effect("keeps", "keeps")
ROWS = Effect("rows", "reset")
ONE = Effect("one", "reset")
ZERO = Effect("zero", "reset")
UNKNOWN = Effect("unknown", "unknown")

BY_KIND: dict[str, Effect] = {
    # variables
    "declare_variable": KEEPS,            # with an initial value it is a SET: see effect()
    "declare_row_set_variable": KEEPS,
    "assign": ONE,
    "assign_from_rows": ROWS,
    # temp tables
    "create_temp_table": ZERO,            # from a query it is a SELECT INTO: see effect()
    "drop_temp_table": ZERO,
    # control flow
    "if": ZERO,
    "while": ZERO,
    "break": KEEPS,
    "continue": ZERO,                     # it goes to the condition of the loop, which evaluates it
    "jump": KEEPS,
    "declare_label": KEEPS,
    "return": ONE,
    # rows
    "emit_result_set": ROWS,
    "insert_rows": ROWS,
    "update_rows": ROWS,
    "delete_rows": ROWS,
    "write_current_row": ROWS,
    "call_procedure": Effect("callee", "reset"),
    # transactions
    "begin_transaction": ZERO,
    "commit_transaction": ZERO,
    "rollback_transaction": ZERO,
    "save_point": ZERO,
    "rollback_to_save_point": ZERO,
    "set_isolation_level": ZERO,
    # cursors
    "declare_cursor": Effect("keeps", "reset"),
    "open_cursor": ZERO,
    "fetch_cursor": Effect("fetch", "reset"),
    "close_cursor": ZERO,
    "deallocate_cursor": ZERO,
    # errors and messages
    "try_catch": ZERO,
    "raise_error": ZERO,
    "rethrow": ZERO,
    "emit_message": ZERO,
    # session options
    "set_session_option": ZERO,
    "set_abort_on_error": ZERO,
    # kept as written: it could be anything
    "opaque_fragment": UNKNOWN,
    "dynamic_sql_execution": UNKNOWN,
    "schema_change": UNKNOWN,
    "permission_change": UNKNOWN,
}


def statement_kinds() -> frozenset[str]:
    """Every kind of node that is a statement of a procedural body and so has an effect."""
    return frozenset(BY_KIND)


def effect(node: Node) -> Effect:
    """What the statement leaves in `@@ROWCOUNT` and `@@ERROR`."""
    if node.kind == "declare_variable" and node.children_with("initial"):
        return ONE                                      # DECLARE @x int = 1 assigns, as SET does
    if node.kind == "create_temp_table" and node.attrs.get("from_query"):
        return ROWS                                     # SELECT ... INTO #t
    try:
        return BY_KIND[node.kind]
    except KeyError:
        raise KeyError(f"{node.kind} is not a statement whose effect on @@ROWCOUNT and @@ERROR is known") from None
