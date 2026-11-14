"""The divider (spec sections 1, 4 and 5): a long artefact is divided along the boundaries of its statements and
nested blocks, never by line count, so that no artefact is rejected for its length. It works on the intermediate
representation and knows nothing of any source.

A **part** is an `Artefact` whose body is a `part_body`: a run of consecutive statements of one block, in source
order, copied with their ids, roles, scopes and source positions. Parts, in order, hold every statement of the
artefact exactly once. The body's attributes say where the run sits and what it needs:

- `index`, `count`: the part's place among the parts;
- `path`: the blocks around the run, outermost first, each `{node, kind, role, span, scope, condition}`: the
  statement the run lies inside, the role the run has in it (`then`, `else`, `body`, `handler`; a batch of a script
  is `statement`), that statement's own scope and the id of its condition node. The statements and conditions
  themselves stay in the artefact the parts were cut from, which remains the document of record;
- `scope`: the scope the first statement starts in, and `start_state`: the whole state it starts in, which `scopes`
  derives from. Deriving from `start_state` gives every statement of the part the scope the whole artefact gave it,
  so the checker verifies a part as it verifies an artefact;
- `declarations`: for each name in that scope, the node that declared it (`node`, None for what the body itself
  declares), with the type of a variable or parameter and the column count of a cursor; `labels`: every label of the
  artefact, because a jump may leave its part;
- `size`: the length in characters of the source text the run spans, and `oversize`: whether that is more than the
  budget. A statement that is itself larger than the budget and has no statements inside it cannot be divided: it
  is a part of its own, flagged, never rejected. What an AI step does with it is the context builder's rule.

The budget is a number of characters of source text: a proxy that is deterministic and does not depend on a model.
The token budget of an AI step is a value the build sets from measured contexts (spec, Build Commitments); the
caller passes the budget it has.

An artefact whose body has no statements (a view or an inline function: one query) cannot be divided along blocks.
It is one part holding the whole body.
"""
from __future__ import annotations

import copy

from . import scopes
from .ir import Artefact, Node, SourceSpan

DECLARING = {"declare_parameter": "parameters", "declare_variable": "variables",
             "declare_row_set_variable": "row_sets", "create_temp_table": "temp_tables",
             "declare_cursor": "cursors"}


def divide(artefact: Artefact, budget: int) -> list[Artefact]:
    """The parts of an artefact, in source order. `budget` is in characters of source text."""
    if budget < 1:
        raise ValueError(f"the budget is a number of characters above zero, not {budget}")
    body = artefact.body
    if not _has_statements(body):
        return [_whole(artefact, budget)]
    states = {id(node): state for node, state in scopes.derive_states(body)}
    runs: list[tuple[list[Node], list[dict]]] = []
    if body.kind == "batch_script_body":
        for batch in body.children_with("batch"):
            _divide(batch.children_with("statement"), [_entry(batch, "statement")], budget, runs)
    else:
        _divide(body.children_with("statement"), [], budget, runs)
    labels = sorted({n.attrs["name"] for n in body.walk() if n.kind == "declare_label"}, key=str.casefold)
    parts = []
    for index, (statements, path) in enumerate(runs):
        first = statements[0]
        start = states[id(first)]
        size = _size(statements)
        attrs = {"index": index, "count": len(runs), "path": path, "scope": first.attrs["scope"],
                 "start_state": start.to_json(), "declarations": _declarations(body, start.scope(), first),
                 "labels": labels, "size": size, "oversize": size > budget}
        part = Node("part_body", _span(statements[0], statements[-1]), None, attrs,
                    [copy.deepcopy(s) for s in statements], id=f"p{index}")
        parts.append(_artefact(artefact, part))
    return parts


def _has_statements(body: Node) -> bool:
    return any(c.role in scopes.STATEMENT_ROLES or c.role == "batch" for c in body.children)


def _whole(artefact: Artefact, budget: int) -> Artefact:
    """The one part of an artefact with no statements: all of its body."""
    body = artefact.body
    size = body.position.length
    attrs = {"index": 0, "count": 1, "path": [], "scope": None, "start_state": scopes.State().to_json(),
             "declarations": [], "labels": [], "size": size, "oversize": size > budget}
    return _artefact(artefact, Node("part_body", body.span, None, attrs, copy.deepcopy(body.children), id="p0"))


def _artefact(parent: Artefact, part: Node) -> Artefact:
    held = {n.id for n in part.walk()}
    return Artefact(parent.identity, parent.artefact_type, parent.original_source, part,
                    dependencies=list(parent.dependencies), vocabulary_version=parent.vocabulary_version,
                    secret_settings=list(parent.secret_settings),
                    hints=[h for h in parent.hints if h["node"] in held],
                    assumptions=list(parent.assumptions))


def _entry(node: Node, role: str) -> dict:
    condition = node.children_with("condition")
    return {"node": node.id, "kind": node.kind, "role": role, "span": str(node.position),
            "scope": node.attrs.get("scope"), "condition": condition[0].id if condition else None}


def _size(statements: list[Node]) -> int:
    first, last = statements[0].position, statements[-1].position
    return last.offset + last.length - first.offset


def _span(first: Node, last: Node) -> SourceSpan:
    a, b = first.position, last.position
    return SourceSpan(a.start_line, a.start_column, b.end_line, b.end_column, a.offset,
                      b.offset + b.length - a.offset)


def _roles(node: Node) -> list[str]:
    """The roles of the statement lists inside a statement, in the order they appear."""
    found: list[str] = []
    for child in node.children:
        if child.role in scopes.STATEMENT_ROLES and child.role not in found:
            found.append(child.role)
    return found


def _divide(block: list[Node], path: list[dict], budget: int, runs: list[tuple[list[Node], list[dict]]]) -> None:
    """Pack the statements of one block into runs. A statement larger than the budget that has statements inside it
    is opened, and its blocks are divided in turn; any other statement larger than the budget is a run of its own,
    because adding a neighbour to it only makes it larger."""
    run: list[Node] = []

    def flush() -> None:
        if run:
            runs.append((list(run), path))
            run.clear()

    for statement in block:
        if statement.position.length > budget and _roles(statement):
            flush()
            for role in _roles(statement):
                _divide(statement.children_with(role), path + [_entry(statement, role)], budget, runs)
            continue
        if run and _size(run + [statement]) > budget:
            flush()
        run.append(statement)
    flush()


def _declarations(body: Node, scope: dict, first: Node) -> list[dict]:
    """The declaration of each name in scope: the last node before the part that declares it."""
    declared: dict[tuple[str, str], Node] = {}
    for node in body.walk():
        group = DECLARING.get(node.kind)
        if group is None or (node.kind != "declare_parameter" and node.position.offset >= first.position.offset):
            continue
        if node.kind == "declare_parameter" and node.attrs.get("row_set"):
            declared[("row_sets", node.attrs["name"].casefold())] = node
        declared[(group, node.attrs["name"].casefold())] = node
    out = []
    for group in ("parameters", "variables", "row_sets", "temp_tables", "cursors"):
        for name in scope[group]:
            node = declared.get((group, name.casefold()))
            if node is None and group == "row_sets" and name == body.attrs.get("return_variable"):
                out.append({"name": name, "group": group, "kind": "return_variable", "node": None, "type": None,
                            "columns": None})
                continue
            if node is None:
                raise ValueError(f"{name} is in the scope of the statement at {first.position} but no statement "
                                 "before it declares it")
            out.append({"name": name, "group": group, "kind": node.kind, "node": node.id,
                        "type": node.type.to_json() if node.type is not None else None,
                        "columns": _cursor_columns(node)})
    return out


def _cursor_columns(node: Node) -> int | None:
    if node.kind != "declare_cursor" or node.attrs["columns"] is None:
        return None
    return len(node.attrs["columns"])
