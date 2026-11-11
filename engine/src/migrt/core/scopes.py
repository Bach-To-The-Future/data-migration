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
  because a barrier came before it. Cursors and open transactions join the scope with issue #85.

Lowering writes the attribute with `annotate`, and the checker derives it again with `derive` and refuses a
body whose attribute differs, so the scope at every point is both stated and verified.
"""
from __future__ import annotations

import dataclasses

from .ir import Node

STATEMENT_ROLES = ("statement", "then", "else", "body")
BARRIERS = ("opaque_fragment", "dynamic_sql_execution", "schema_change", "permission_change")


@dataclasses.dataclass
class State:
    parameters: list[str] = dataclasses.field(default_factory=list)
    variables: list[str] = dataclasses.field(default_factory=list)
    row_sets: list[str] = dataclasses.field(default_factory=list)
    temp_tables: list[str] = dataclasses.field(default_factory=list)
    barrier: str | None = None

    def copy(self) -> "State":
        return State(list(self.parameters), list(self.variables), list(self.row_sets), list(self.temp_tables),
                     self.barrier)

    def scope(self) -> dict:
        return {"parameters": list(self.parameters), "variables": list(self.variables),
                "row_sets": list(self.row_sets), "temp_tables": list(self.temp_tables), "barrier": self.barrier}


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


def _visit(node: Node, state: State, out: list[tuple[Node, dict]]) -> None:
    out.append((node, state.scope()))
    kind = node.kind
    if kind == "declare_variable":
        _add(state.variables, node.attrs["name"])
    elif kind == "declare_row_set_variable":
        _add(state.row_sets, node.attrs["name"])
    elif kind == "create_temp_table":
        _add(state.temp_tables, node.attrs["name"])
    elif kind == "drop_temp_table":
        state.temp_tables[:] = [t for t in state.temp_tables if t.casefold() != node.attrs["name"].casefold()]
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


def _join(state: State, *paths: State) -> None:
    state.variables[:] = _merge(state.variables, *[p.variables for p in paths])
    state.row_sets[:] = _merge(state.row_sets, *[p.row_sets for p in paths])
    state.temp_tables[:] = _merge(state.temp_tables, *[p.temp_tables for p in paths])
    state.barrier = next((p.barrier for p in paths if p.barrier), state.barrier)


def derive(body: Node) -> list[tuple[Node, dict]]:
    """Every statement node of a body with the scope it starts in, in order."""
    out: list[tuple[Node, dict]] = []
    declared = body.children_with("parameter")
    state = State(parameters=[c.attrs["name"] for c in declared],
                  row_sets=[c.attrs["name"] for c in declared if c.attrs.get("row_set")])   # table-valued parameters
    if body.attrs.get("return_variable"):
        state.row_sets.append(body.attrs["return_variable"])     # the row set a multi-statement function fills
    if body.kind == "batch_script_body":
        for batch in body.children_with("batch"):
            state.variables, state.row_sets = [], []           # variables do not survive GO; temp tables do
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
    if node.kind in ("variable", "assign", "assign_from_rows", "call_procedure", "drop_temp_table"):
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


def known(scope: dict, name: str, *, row_set: bool = False) -> bool:
    """Whether a variable (or, with `row_set`, a table variable) is in `scope`."""
    if row_set:
        return _has(scope["row_sets"], name)
    return _has(scope["parameters"], name) or _has(scope["variables"], name)


def temp_table_known(scope: dict, name: str) -> bool:
    return _has(scope["temp_tables"], name)
