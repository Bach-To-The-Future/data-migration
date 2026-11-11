"""Explicit scopes (spec section 4, item 10): what is in scope before every statement.

Every statement node of a procedural body carries a `scope` attribute: the parameters, the variables, the
row-set variables (table variables) and the temp tables that are in scope when it starts, and `barrier`, the
position of the most recent scope barrier before it (None if there is none). The rules are T-SQL's, stated as
facts about the representation, not about the source:

- a variable is declared for the rest of its batch, not its block: a declaration inside a branch or a loop
  stays in scope after it;
- a temp table is created and dropped when the statements run, so after a branch or a loop it *may exist*: a
  table made in either branch may exist after it, and a table dropped in one branch may still exist;
- a batch of a script starts with no variables (they do not survive `GO`) and keeps the temp tables;
- everything after a scope barrier is uncertain: the barrier may have declared, changed or dropped anything.

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


def annotate(body: Node) -> None:
    for node, scope in derive(body):
        node.attrs = dict(node.attrs, scope=scope)


def known(scope: dict, name: str, *, row_set: bool = False) -> bool:
    """Whether a variable (or, with `row_set`, a table variable) is in `scope`."""
    if row_set:
        return _has(scope["row_sets"], name)
    return _has(scope["parameters"], name) or _has(scope["variables"], name)


def temp_table_known(scope: dict, name: str) -> bool:
    return _has(scope["temp_tables"], name)
