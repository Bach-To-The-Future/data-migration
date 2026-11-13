"""Source comparisons against error numbers (spec section 6, "Errors": "any source comparison against a number the mapping
doesn't cover is flagged").

Generated code raises only the numbers the SQL Server half of the error mapping gives, plus the numbers the program raises
itself. A handler that compares `ERROR_NUMBER()` or `@@ERROR` with any other number, a deadlock's 1205 for one, tests for an
error the generated code can never show it: the comparison is false where the original's was true. It is found here, in the
parsed source, and flagged at the artefact in the report.

A comparison is found when one side is `ERROR_NUMBER()`, `@@ERROR` or a variable that is assigned from either anywhere in
the artefact, and the other is a whole number: with `=`, `<>`, the other operators, `BETWEEN`, `IN` and `NOT IN`, and the two forms of
`CASE`. A number held in a variable or computed is not found; the report cannot say what it is."""
from __future__ import annotations

import dataclasses

from ...core.ir import SourceSpan
from . import error_mapping
from .expressions import type_name
from .parser import Parsed, walk


@dataclasses.dataclass(frozen=True)
class Comparison:
    number: int
    position: SourceSpan
    text: str


def _is_error_source(fragment, variables: set[str]) -> bool:
    kind = type_name(fragment)
    if kind == "FunctionCall":
        return str(fragment.FunctionName.Value).upper() == "ERROR_NUMBER" and fragment.CallTarget is None
    if kind == "GlobalVariableExpression":
        return str(fragment.Name).upper() == "@@ERROR"
    if kind == "VariableReference":
        return str(fragment.Name).casefold() in variables
    if kind == "ParenthesisExpression":
        return _is_error_source(fragment.Expression, variables)
    return False


def _whole_number(fragment) -> int | None:
    kind = type_name(fragment)
    if kind == "IntegerLiteral":
        return int(str(fragment.Value))
    if kind == "UnaryExpression" and type_name(fragment.Expression) == "IntegerLiteral":
        value = int(str(fragment.Expression.Value))
        return -value if str(fragment.UnaryExpressionType) == "Negative" else value
    if kind == "ParenthesisExpression":
        return _whole_number(fragment.Expression)
    return None


def _error_variables(parsed: Parsed) -> set[str]:
    """Variables assigned from `@@ERROR` or `ERROR_NUMBER()` anywhere in the artefact (by name: a deliberate over-reach)."""
    found: set[str] = set()

    def visit(node) -> None:
        kind = type_name(node)
        if kind in ("SetVariableStatement", "SelectSetVariable") and node.Variable is not None:
            if _is_error_source(node.Expression, set()):
                found.add(str(node.Variable.Name).casefold())
        elif kind == "DeclareVariableElement" and node.Value is not None and _is_error_source(node.Value, set()):
            found.add(str(node.VariableName.Value).casefold())

    walk(parsed.tree, visit)
    return found


def find(parsed: Parsed) -> list[Comparison]:
    variables = _error_variables(parsed)
    found: dict[tuple[int, int, int], Comparison] = {}       # the walk visits a node once for each of its base types

    def add(fragment, number: int | None) -> None:
        if number is not None:
            span = parsed.span(fragment)
            found.setdefault((span.offset, span.length, number),
                             Comparison(number, span, parsed.text[span.offset:span.offset + span.length]))

    def visit(node) -> None:
        kind = type_name(node)
        if kind == "BooleanComparisonExpression":
            if _is_error_source(node.FirstExpression, variables):
                add(node, _whole_number(node.SecondExpression))
            elif _is_error_source(node.SecondExpression, variables):
                add(node, _whole_number(node.FirstExpression))
        elif kind == "InPredicate" and node.Subquery is None and _is_error_source(node.Expression, variables):
            for value in node.Values:
                add(node, _whole_number(value))
        elif kind == "BooleanTernaryExpression" and str(node.TernaryExpressionType) in ("Between", "NotBetween")                 and _is_error_source(node.FirstExpression, variables):
            add(node, _whole_number(node.SecondExpression))
            add(node, _whole_number(node.ThirdExpression))
        elif kind == "SimpleCaseExpression" and _is_error_source(node.InputExpression, variables):
            for clause in node.WhenClauses:
                add(clause, _whole_number(clause.WhenExpression))

    walk(parsed.tree, visit)
    return sorted(found.values(), key=lambda c: (c.position.offset, c.number))


def flagged(comparisons: list[Comparison]) -> list[Comparison]:
    return [c for c in comparisons if not error_mapping.covers(c.number)]


def flag(comparison: Comparison) -> dict:
    """The report's entry for a flagged comparison."""
    return {
        "kind": "unmapped_error_number",
        "number": comparison.number,
        "source_position": str(comparison.position),
        "text": (f"The code compares an error number with {comparison.number}, which the error mapping does not cover: "
                 f"generated code cannot raise it, so this comparison is never true there. A failure the mapping does not "
                 f"cover is reported with the reserved number {error_mapping.reserved_number()}."),
    }
