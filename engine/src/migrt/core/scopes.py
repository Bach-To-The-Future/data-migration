"""Explicit scopes (spec section 4, item 10): what is in scope before every statement.

Every statement node of a procedural body carries a `scope` attribute: the parameters, the variables, the
row-set variables and the session-visible temporary tables that are in scope when it starts, and `barrier`, the
position of the most recent scope barrier before it (None if there is none). The rules are facts about the
representation, not about one source:

- a variable is declared for the rest of its batch, not its block: a declaration inside a branch or a loop
  stays in scope after it;
- a temporary table is created and dropped when the statements run, so after a branch or a loop it *may
  exist*: a table made in either branch may exist after it, and a table dropped in one branch may still exist.
  The lists hold what may be in scope, not what is certain;
- a batch of a script starts with no variables and keeps the temporary tables;
- a scope barrier may have declared, changed or dropped anything, so the lists after one may be incomplete or
  stale. The scope says so with `barrier`; it does not guess, and the checker does not refuse a name only
  because a barrier came before it;
- a cursor is in scope from its declaration to its deallocation, and open from `open_cursor` to `close_cursor`
  (`cursors`, `open_cursors`: the cursors that may be, as for temp tables). A local cursor ends with its batch, a
  global one does not;
- the open transactions are a range, `transaction`: the fewest and the most levels that may be open (`max` is None
  where the caller may have opened any number), the `BEGIN` statements whose transaction may still be open, and the
  savepoints that may be set. A procedure may run inside its caller's transaction, a trigger always runs inside one,
  and a called procedure may end the transaction or leave one open, so a call makes the range `[0, None]`;
- a rollback of the whole transaction undoes the creation of the temp tables made inside it and brings back the ones
  dropped inside it (both are transactional in SQL Server), and a rollback to a savepoint does the same for what came
  after the savepoint and leaves the transaction open. It never undoes a table variable's rows or its declaration: a
  table variable is not transactional. What a commit of the outermost level closes is permanent;
- a TRY block may stop after any of its statements, so the handler starts in what may be in scope after any of them,
  and after the whole statement what may be in scope after the block or after the handler. Nothing in it is a scope
  barrier.

Lowering writes the attribute with `annotate`, and the checker derives it again with `derive` and refuses a
body whose attribute differs, so the scope at every point is both stated and verified.
"""
from __future__ import annotations

import dataclasses

from .ir import Node

STATEMENT_ROLES = ("statement", "then", "else", "body", "handler")
BARRIERS = ("opaque_fragment", "dynamic_sql_execution", "schema_change", "permission_change")
TRANSACTION_KINDS = ("begin_transaction", "commit_transaction", "rollback_transaction", "save_point",
                     "rollback_to_save_point")
CURSOR_KINDS = ("declare_cursor", "open_cursor", "close_cursor", "deallocate_cursor")


@dataclasses.dataclass
class State:
    parameters: list[str] = dataclasses.field(default_factory=list)
    variables: list[str] = dataclasses.field(default_factory=list)
    row_sets: list[str] = dataclasses.field(default_factory=list)
    temp_tables: list[str] = dataclasses.field(default_factory=list)
    barrier: str | None = None
    cursors: list[str] = dataclasses.field(default_factory=list)
    open_cursors: list[str] = dataclasses.field(default_factory=list)
    global_cursors: list[str] = dataclasses.field(default_factory=list)       # not in the scope: which outlive a batch
    tx_min: int = 0
    tx_max: int | None = 0
    begun: list[str] = dataclasses.field(default_factory=list)
    save_points: list[str] = dataclasses.field(default_factory=list)
    # What a rollback would undo or bring back, in order, while a transaction is certainly open: ("created", name,
    # savepoints set so far) and ("dropped", name, savepoints set so far). Not in the scope.
    journal: list[tuple[str, str, int]] = dataclasses.field(default_factory=list)

    def copy(self) -> "State":
        return dataclasses.replace(
            self, parameters=list(self.parameters), variables=list(self.variables), row_sets=list(self.row_sets),
            temp_tables=list(self.temp_tables), cursors=list(self.cursors), open_cursors=list(self.open_cursors),
            global_cursors=list(self.global_cursors), begun=list(self.begun), save_points=list(self.save_points),
            journal=list(self.journal))

    def scope(self) -> dict:
        return {"parameters": list(self.parameters), "variables": list(self.variables),
                "row_sets": list(self.row_sets), "temp_tables": list(self.temp_tables),
                "cursors": list(self.cursors), "open_cursors": list(self.open_cursors),
                "transaction": {"min": self.tx_min, "max": self.tx_max, "begun": list(self.begun),
                                "save_points": list(self.save_points)},
                "barrier": self.barrier}


def _has(names: list[str], name: str) -> bool:
    return any(n.casefold() == name.casefold() for n in names)


def _add(names: list[str], name: str) -> None:
    if not _has(names, name):
        names.append(name)


def _merge(before: list[str], *branches: list[str]) -> list[str]:
    """The names that may exist after alternative paths: those of `before` that survive on some path, then the
    ones some path added."""
    out = [n for n in before if any(_has(b, n) for b in branches)]
    for branch in branches:
        for n in branch:
            _add(out, n)
    return out


def _statements(node: Node, *roles: str) -> list[Node]:
    return [c for c in node.children if c.role in roles]


def _remove(names: list[str], name: str) -> None:
    names[:] = [n for n in names if n.casefold() != name.casefold()]


def _journal(state: State, entry: str, name: str) -> None:
    state.journal.append((entry, name, len(state.save_points)))


def _forget_creations(state: State) -> None:
    """After something that may have ended the transaction, no creation is certainly undone by a later rollback."""
    state.journal = [e for e in state.journal if e[0] != "created"]


def _end_transaction(state: State) -> None:
    state.tx_min, state.tx_max = 0, 0
    state.begun, state.save_points, state.journal = [], [], []


def _undo(state: State, after: int) -> None:
    """Roll back what came after the first `after` savepoints: bring back what was dropped, remove what was created
    (when a transaction is certainly open; otherwise a creation may not have been inside one)."""
    kept = []
    for entry, name, saved in reversed(state.journal):
        if saved < after:
            kept.append((entry, name, saved))
        elif entry == "dropped":
            _add(state.temp_tables, name)
        elif state.tx_min > 0:
            _remove(state.temp_tables, name)
    state.journal = list(reversed(kept))


def _transaction(node: Node, state: State) -> None:
    kind = node.kind
    if kind == "begin_transaction":
        state.tx_min += 1
        state.tx_max = None if state.tx_max is None else state.tx_max + 1
        state.begun.append(str(node.span))
    elif kind == "commit_transaction":
        state.tx_min = max(state.tx_min - 1, 0)
        state.tx_max = None if state.tx_max is None else max(state.tx_max - 1, 0)
        if state.tx_max == 0:
            _end_transaction(state)
        elif state.tx_min == 0:
            _forget_creations(state)
    elif kind == "rollback_transaction":
        _undo(state, 0)
        _end_transaction(state)
    elif kind == "save_point":
        state.save_points.append(node.attrs["name"])
    elif kind == "rollback_to_save_point":
        found = [i for i, n in enumerate(state.save_points) if n.casefold() == node.attrs["save_point"].casefold()]
        if found:
            _undo(state, found[-1] + 1)
            del state.save_points[found[-1] + 1:]
    elif kind == "call_procedure":                   # it may end the transaction, or leave one open
        state.tx_min, state.tx_max = 0, None
        _forget_creations(state)


def _cursor(node: Node, state: State) -> None:
    kind = node.kind
    if kind == "declare_cursor":
        _add(state.cursors, node.attrs["name"])
        if node.attrs["cursor_scope"] != "local":
            _add(state.global_cursors, node.attrs["name"])
    elif kind == "open_cursor":
        _add(state.open_cursors, node.attrs["cursor"])
    elif kind == "close_cursor":
        _remove(state.open_cursors, node.attrs["cursor"])
    elif kind == "deallocate_cursor":
        for names in (state.cursors, state.open_cursors, state.global_cursors):
            _remove(names, node.attrs["cursor"])


def _visit(node: Node, state: State, out: list[tuple[Node, dict]]) -> None:
    out.append((node, state.scope()))
    kind = node.kind
    if kind == "declare_variable":
        _add(state.variables, node.attrs["name"])
    elif kind == "declare_row_set_variable":
        _add(state.row_sets, node.attrs["name"])
    elif kind == "create_temp_table":
        _add(state.temp_tables, node.attrs["name"])
        if state.tx_min > 0:
            _journal(state, "created", node.attrs["name"])
    elif kind == "drop_temp_table":
        _remove(state.temp_tables, node.attrs["name"])
        if state.tx_max is None or state.tx_max > 0:
            _journal(state, "dropped", node.attrs["name"])
    elif kind in TRANSACTION_KINDS or kind == "call_procedure":
        _transaction(node, state)
    elif kind in CURSOR_KINDS:
        _cursor(node, state)
    elif kind in BARRIERS and node.attrs.get("scope_barrier") is True:
        state.barrier = str(node.span)
    elif kind == "if":
        then_state, else_state = state.copy(), state.copy()
        for child in _statements(node, "then"):
            _visit(child, then_state, out)
        for child in _statements(node, "else"):
            _visit(child, else_state, out)
        _join(state, then_state, else_state)
    elif kind == "while":
        body_state = state.copy()
        for child in _statements(node, "body"):
            _visit(child, body_state, out)
        _join(state, body_state, state.copy())     # the body may run no times
    elif kind == "try_catch":
        block, handler = state.copy(), state.copy()
        for child in _statements(node, "body"):
            _visit(child, block, out)
            _join(handler, handler.copy(), block)      # the error may come after any statement of the block
        for child in _statements(node, "handler"):
            _visit(child, handler, out)
        _join(state, block, handler)


def _join(state: State, *paths: State) -> None:
    state.variables[:] = _merge(state.variables, *[p.variables for p in paths])
    state.row_sets[:] = _merge(state.row_sets, *[p.row_sets for p in paths])
    state.temp_tables[:] = _merge(state.temp_tables, *[p.temp_tables for p in paths])
    state.cursors[:] = _merge(state.cursors, *[p.cursors for p in paths])
    state.open_cursors[:] = _merge(state.open_cursors, *[p.open_cursors for p in paths])
    state.global_cursors[:] = _merge(state.global_cursors, *[p.global_cursors for p in paths])
    state.tx_min = min(p.tx_min for p in paths)
    state.tx_max = None if any(p.tx_max is None for p in paths) else max(p.tx_max or 0 for p in paths)
    state.begun[:] = _merge([], *[p.begun for p in paths])
    state.save_points[:] = _merge([], *[p.save_points for p in paths])
    journals = [p.journal for p in paths]
    if all(j == journals[0] for j in journals):
        state.journal = list(journals[0])
    else:                                          # which transaction a creation was in is no longer certain
        dropped = [e for j in journals for e in j if e[0] == "dropped"]
        state.journal = [e for i, e in enumerate(dropped) if e not in dropped[:i]]
    state.barrier = next((p.barrier for p in paths if p.barrier), state.barrier)


def derive(body: Node) -> list[tuple[Node, dict]]:
    """Every statement node of a body with the scope it starts in, in order."""
    out: list[tuple[Node, dict]] = []
    declared = body.children_with("parameter")
    state = State(parameters=[c.attrs["name"] for c in declared],
                  row_sets=[c.attrs["name"] for c in declared if c.attrs.get("row_set")])   # table-valued parameters
    if body.kind == "trigger_body":
        state.tx_min, state.tx_max = 1, None            # a trigger runs inside the transaction of the statement that fired it
    elif body.kind != "batch_script_body":
        state.tx_min, state.tx_max = 0, None            # the caller may have a transaction open
    if body.attrs.get("return_variable"):
        state.row_sets.append(body.attrs["return_variable"])     # the row set a multi-statement function fills
    if body.kind == "batch_script_body":
        for batch in body.children_with("batch"):
            state.variables, state.row_sets = [], []           # variables do not survive GO; temp tables do
            state.cursors = [c for c in state.cursors if _has(state.global_cursors, c)]       # nor do local cursors
            state.open_cursors = [c for c in state.open_cursors if _has(state.global_cursors, c)]
            for child in _statements(batch, "statement"):
                _visit(child, state, out)
    else:
        for child in _statements(body, "statement"):
            _visit(child, state, out)
    return out


WRITES = ("insert_rows", "update_rows", "delete_rows")


def depends_on_scope(node: Node) -> bool:
    """Whether the node's meaning depends on an entry of the scope: a variable, a table variable or a temp table it
    reads, writes, drops or hands to a called procedure."""
    if node.kind in ("variable", "assign", "assign_from_rows", "call_procedure", "drop_temp_table", "open_cursor",
                     "fetch_cursor", "close_cursor", "deallocate_cursor", "write_current_row", "commit_transaction",
                     "rollback_transaction", "save_point", "rollback_to_save_point", "transaction_depth",
                     "transaction_state"):
        return True
    if node.kind == "read_table":
        return bool(node.attrs.get("temp_table") or node.attrs.get("row_set"))
    if node.kind in WRITES:
        return node.attrs["target_kind"] in ("temp_table", "row_set")
    return node.kind == "opaque_fragment" and not node.attrs.get("scope_barrier") and any(
        node.attrs.get("touches", {}).get(k) for k in ("variables", "temp_tables"))


def _own(statement: Node):
    """The statement and the nodes below it that are not statements of their own (those have their own scope)."""
    yield statement
    for child in statement.children:
        if child.role not in STATEMENT_ROLES:
            yield from _own_below(child)


def _own_below(node: Node):
    yield node
    for child in node.children:
        if child.role not in STATEMENT_ROLES:
            yield from _own_below(child)


def stale(body: Node) -> list[tuple[Node, str]]:
    """The nodes that depend on a scope entry that may be stale, with the position of the barrier that made it so.
    After a scope barrier every entry may be stale (it may have been declared, changed or dropped); a node
    carries `scope_may_be_stale` so that generation handles it explicitly or attaches a caveat, and the report
    shows it at the construct."""
    out: list[tuple[Node, str]] = []
    for statement, scope in derive(body):
        if scope["barrier"]:
            out += [(n, scope["barrier"]) for n in _own(statement) if depends_on_scope(n)]
    return out


def annotate(body: Node) -> None:
    for node, scope in derive(body):
        node.attrs = dict(node.attrs, scope=scope)
    for node, barrier in stale(body):
        node.attrs = dict(node.attrs, scope_may_be_stale=barrier)


def cursor_known(scope: dict, name: str) -> bool:
    return _has(scope["cursors"], name)


def known(scope: dict, name: str, *, row_set: bool = False) -> bool:
    """Whether a variable (or, with `row_set`, a table variable) is in `scope`."""
    if row_set:
        return _has(scope["row_sets"], name)
    return _has(scope["parameters"], name) or _has(scope["variables"], name)


def temp_table_known(scope: dict, name: str) -> bool:
    return _has(scope["temp_tables"], name)
