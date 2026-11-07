"""Lowering for SQL Server views, first cut (spec sections 4 and 5).

Deterministic and faithful: every node gets the vocabulary operation that means exactly
what the T-SQL means, typed from the catalog, with its source position. What this cut
cannot express is kept verbatim as an opaque fragment with its reason, never dropped:
the whole query when its shape is not handled, otherwise the one select item.

Handled: one query specification over named tables and qualified joins, WHERE, GROUP BY,
column references, integer literals, + - * / % on integers and money, COUNT, SUM, AVG,
MIN, MAX, ISNULL, NULLIF, and comparisons that are not between strings (string
comparisons need their comparison rule from collation precedence: later work).
"""
from __future__ import annotations

from ...core.catalog import CatalogReader
from ...core.ir import UNKNOWN, Artefact, Node, VType
from ...core import vocabulary
from . import names
from . import types as T
from .parser import Parsed

AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX"}
BINARY = {"Add": "add", "Subtract": "subtract", "Multiply": "multiply", "Divide": "divide", "Modulo": "modulo"}
COMPARISONS = {"Equals": "=", "NotEqualToBrackets": "<>", "NotEqualToExclamation": "<>", "GreaterThan": ">",
               "LessThan": "<", "GreaterThanOrEqualTo": ">=", "LessThanOrEqualTo": "<="}
JOINS = {"Inner": "inner", "LeftOuter": "left", "RightOuter": "right", "FullOuter": "full"}


class Opaque(Exception):
    """Raised inside lowering when a construct is not handled; caught at the nearest point
    that can keep the source verbatim as an opaque fragment."""

    def __init__(self, fragment, reason: str):
        self.fragment, self.reason = fragment, reason
        super().__init__(reason)


def type_name(fragment) -> str:
    return str(fragment.GetType().Name)


class ViewLowering:
    def __init__(self, reader: CatalogReader, identity: str, parsed: Parsed):
        self.reader = reader
        self.identity = identity
        self.parsed = parsed
        self.database, schema, _name = identity.split(".")
        collation = reader.value(self.database, "collation")
        self.name_context = {"database": self.database, "schemas": [schema, "dbo"],
                             "case_sensitive": names.identifiers_case_sensitive(collation)}
        self.scope: dict[str, dict] = {}   # alias -> {"table": identity, "columns": {name: column}}

    # ------------------------------------------------------------------ artefact

    def lower(self) -> Artefact:
        [batch] = list(self.parsed.tree.Batches)
        [statement] = list(batch.Statements)
        if type_name(statement) not in ("CreateViewStatement", "CreateOrAlterViewStatement"):
            raise ValueError(f"{self.identity} is not a CREATE VIEW statement")
        select = statement.SelectStatement
        try:
            if statement.Columns is not None and statement.Columns.Count:
                raise Opaque(select, "a view's column list (CREATE VIEW v (a, b)) is not lowered yet")
            if not self.reader.value(self.identity, "ansi_nulls", "sqlserver"):
                raise Opaque(select, "a view created with ANSI_NULLS OFF is not lowered yet")
            plan = self.query(select)
        except Opaque as opaque:
            plan = self.opaque(select, opaque.reason)
        plan.role = "query"
        body = Node("view_body", self.parsed.span(statement), children=[plan],
                    attrs={"name": self.identity})
        artefact = Artefact(self.identity, "view", self.parsed.text, body,
                            dependencies=sorted({n.attrs["table"] for n in body.walk() if n.kind == "read_table"}),
                            vocabulary_version=vocabulary.version())
        self.attach_comments(body)
        artefact.number_nodes()
        return artefact

    def opaque(self, fragment, reason: str, role: str = "") -> Node:
        span = self.parsed.span(fragment)
        text = self.parsed.text[span.offset:span.offset + span.length]
        return Node("opaque_fragment", span, UNKNOWN, {"reason": reason, "text": text}, role=role)

    def attach_comments(self, body: Node) -> None:
        """A comment annotates the nearest node after it: a select item or a plan step."""
        anchors = sorted((n for n in body.walk() if n.role in ("output", "input", "left", "right", "query")),
                         key=lambda n: n.position.offset)
        for comment in self.parsed.comments:
            after = [n for n in anchors if n.position.offset >= comment.offset]
            (after[0] if after else body).comments.append(comment.text.rstrip())

    # ------------------------------------------------------------------ queries

    def query(self, select) -> Node:
        if select.WithCtesAndXmlNamespaces is not None:
            raise Opaque(select, "common table expressions are not lowered yet")
        if select.OptimizerHints is not None and select.OptimizerHints.Count:
            raise Opaque(select, "query hints are not lowered yet")
        spec = select.QueryExpression
        if type_name(spec) != "QuerySpecification":
            raise Opaque(spec, f"{type_name(spec)} is not lowered yet")
        for prop, what in (("TopRowFilter", "TOP"), ("OrderByClause", "ORDER BY"), ("HavingClause", "HAVING"),
                           ("ForClause", "FOR"), ("OffsetClause", "OFFSET"), ("Into", "INTO")):
            if getattr(spec, prop, None) is not None:
                raise Opaque(spec, f"{what} is not lowered yet")
        if str(spec.UniqueRowFilter) == "Distinct":
            raise Opaque(spec, "SELECT DISTINCT is not lowered yet")
        if spec.FromClause is None:
            raise Opaque(spec, "a query without FROM is not lowered yet")

        refs = list(spec.FromClause.TableReferences)
        if len(refs) != 1:
            raise Opaque(spec.FromClause, "comma-separated FROM lists are not lowered yet")
        plan = self.table_reference(refs[0])
        if spec.WhereClause is not None:
            condition = self.predicate(spec.WhereClause.SearchCondition)
            plan = Node("filter", self.parsed.span(spec.WhereClause), children=[_as(plan, "input"), _as(condition, "condition")])

        items = list(spec.SelectElements)
        grouping = spec.GroupByClause
        aggregating = grouping is not None or any(self.has_aggregate(i) for i in items)
        children = [_as(plan, "input")]
        if grouping is not None:
            if str(grouping.GroupByOption) != "None" or grouping.All:
                raise Opaque(grouping, "GROUP BY options are not lowered yet")
            for spec_item in grouping.GroupingSpecifications:
                if type_name(spec_item) != "ExpressionGroupingSpecification":
                    raise Opaque(spec_item, f"{type_name(spec_item)} is not lowered yet")
                children.append(_as(self.expression(spec_item.Expression), "key"))
        for item in items:
            children.append(self.select_item(item))
        span = self.parsed.span(spec)
        return Node("aggregate" if aggregating else "project", span, children=children)

    def table_reference(self, ref) -> Node:
        kind = type_name(ref)
        if kind == "NamedTableReference":
            obj = ref.SchemaObject
            if obj.ServerIdentifier is not None:
                raise Opaque(ref, "a linked-server table is an external reference, not lowered yet")
            parts = tuple(str(i.Value) for i in obj.Identifiers)
            context = dict(self.name_context, kinds={"table"})
            try:
                table = self.reader.resolve(parts, context)
            except names.AmbiguousName as ambiguous:
                raise Opaque(ref, str(ambiguous)) from None
            if table is None:
                raise Opaque(ref, f"{'.'.join(parts)} does not resolve to a table in the catalog")
            if ref.TableHints is not None and ref.TableHints.Count:
                raise Opaque(ref, "table hints are not lowered yet")
            alias = str(ref.Alias.Value) if ref.Alias is not None else parts[-1]
            columns = {c["name"]: c for c in self.reader.value(table, "columns")}
            key = alias if self.name_context["case_sensitive"] else alias.casefold()
            if key in self.scope:
                raise Opaque(ref, f"alias {alias} is used twice")
            self.scope[key] = {"alias": alias, "table": table, "columns": columns}
            return Node("read_table", self.parsed.span(ref), attrs={"table": table, "alias": alias})
        if kind == "QualifiedJoin":
            join_kind = JOINS.get(str(ref.QualifiedJoinType))
            if join_kind is None or ref.JoinHint.ToString() != "None":
                raise Opaque(ref, f"{ref.QualifiedJoinType} join is not lowered yet")
            left = self.table_reference(ref.FirstTableReference)
            right = self.table_reference(ref.SecondTableReference)
            condition = self.predicate(ref.SearchCondition)
            return Node("join", self.parsed.span(ref), attrs={"kind": join_kind},
                        children=[_as(left, "left"), _as(right, "right"), _as(condition, "condition")])
        raise Opaque(ref, f"{kind} is not lowered yet")

    def select_item(self, item) -> Node:
        if type_name(item) != "SelectScalarExpression":
            raise Opaque(item, f"{type_name(item)} (such as SELECT *) is not lowered yet")
        expression = item.Expression
        if item.ColumnName is not None:
            name = str(item.ColumnName.Value)
        elif type_name(expression) == "ColumnReferenceExpression":
            name = str(list(expression.MultiPartIdentifier.Identifiers)[-1].Value)
        else:
            raise Opaque(item, "a view column needs a name")
        try:
            node = self.expression(expression)
        except Opaque as opaque:
            node = self.opaque(item, opaque.reason)
        node.span = self.parsed.span(item)
        node.role = "output"
        node.attrs = dict(node.attrs, output_name=name)
        return node

    # ------------------------------------------------------------------ expressions

    def has_aggregate(self, fragment) -> bool:
        found = []

        def visit(f):
            if type_name(f) == "FunctionCall" and str(f.FunctionName.Value).upper() in AGGREGATES:
                found.append(f)
        _walk(fragment, visit)
        return bool(found)

    def expression(self, e) -> Node:
        kind = type_name(e)
        span = self.parsed.span(e)
        if kind == "ParenthesisExpression":
            return self.expression(e.Expression)
        if kind == "ColumnReferenceExpression":
            return self.column(e)
        if kind == "IntegerLiteral":
            value = int(str(e.Value))
            if not -2**31 <= value < 2**31:
                raise Opaque(e, "an integer literal outside int becomes numeric: not lowered yet")
            return Node("literal", span, T.INT, {"value": value})
        if kind == "BinaryExpression":
            return self.binary(e, span)
        if kind == "NullIfExpression":
            first, second = self.expression(e.FirstExpression), self.expression(e.SecondExpression)
            self.require_comparable(e, first, second)
            return Node("null_if_equal", span, first.type, {"rule": None},
                        [_as(first, "argument"), _as(second, "argument")])
        if kind == "FunctionCall":
            return self.function(e, span)
        raise Opaque(e, f"{kind} is not lowered yet")

    def column(self, e) -> Node:
        if str(e.ColumnType) != "Regular":
            raise Opaque(e, f"{e.ColumnType} column reference is not lowered yet")
        parts = [str(i.Value) for i in e.MultiPartIdentifier.Identifiers]
        fold = (lambda s: s) if self.name_context["case_sensitive"] else str.casefold
        if len(parts) == 2:
            entry = self.scope.get(fold(parts[0]))
            candidates = [entry] if entry else []
        elif len(parts) == 1:
            candidates = list(self.scope.values())
        else:
            raise Opaque(e, "a column named with more than two parts is not lowered yet")
        matches = [(s, c) for s in candidates for name, c in s["columns"].items() if fold(name) == fold(parts[-1])]
        if len(matches) != 1:
            raise Opaque(e, f"column {'.'.join(parts)} does not resolve to exactly one column in scope")
        scope, column = matches[0]
        return Node("column", self.parsed.span(e), T.from_column(column),
                    {"relation": scope["alias"], "table": scope["table"], "column": column["name"]})

    def binary(self, e, span) -> Node:
        op = BINARY.get(str(e.BinaryExpressionType))
        if op is None:
            raise Opaque(e, f"{e.BinaryExpressionType} is not lowered yet")
        left, right = self.expression(e.FirstExpression), self.expression(e.SecondExpression)
        args = [_as(left, "argument"), _as(right, "argument")]
        if T.is_integer(left.type) and T.is_integer(right.type):
            result = T.wider_integer(left.value_type, right.value_type)
            operation = {"divide": "integer_divide"}.get(op, op)
            return Node(operation, span, result, children=args)
        money = [t for t in (left.value_type, right.value_type) if T.is_money(t)]
        others = [t for t in (left.value_type, right.value_type) if not T.is_money(t)]
        if money and all(T.is_integer(t) for t in others):
            result = max(money, key=lambda t: t.int_param("precision"))
            operation = {"multiply": "money_multiply", "divide": "money_divide"}.get(op, op)
            if operation == "modulo":
                raise Opaque(e, "modulo on money is not lowered yet")
            return Node(operation, span, result, children=args)
        raise Opaque(e, f"{op} on {left.type} and {right.type} is not lowered yet")

    def function(self, e, span) -> Node:
        if e.CallTarget is not None:
            raise Opaque(e, "a call to a user-defined function is not lowered yet")
        if e.OverClause is not None:
            raise Opaque(e, "window functions are not lowered yet")
        name = str(e.FunctionName.Value).upper()
        params = list(e.Parameters)
        distinct = str(e.UniqueRowFilter) == "Distinct"
        if name == "COUNT" and len(params) == 1:
            p = params[0]
            if type_name(p) == "ColumnReferenceExpression" and str(p.ColumnType) == "Wildcard":
                return Node("count_rows", span, T.INT)
            return Node("count_values", span, T.INT, {"distinct": distinct}, [_as(self.expression(p), "argument")])
        if distinct:
            raise Opaque(e, f"{name}(DISTINCT ...) is not lowered yet")
        if name in ("SUM", "AVG") and len(params) == 1:
            arg = self.expression(params[0])
            result = T.sum_type(arg.value_type)
            if result is None:
                raise Opaque(e, f"{name} over {arg.type} is not lowered yet")
            return Node("sum" if name == "SUM" else "average", span, result, children=[_as(arg, "argument")])
        if name in ("MIN", "MAX") and len(params) == 1:
            arg = self.expression(params[0])
            if arg.value_type.name == "string":
                raise Opaque(e, f"{name} over strings needs the collation's ordering: not lowered yet")
            return Node("minimum" if name == "MIN" else "maximum", span, arg.type, children=[_as(arg, "argument")])
        if name == "ISNULL" and len(params) == 2:
            first, second = self.expression(params[0]), self.expression(params[1])
            self.require_comparable(e, first, second)
            # ISNULL takes the first argument's type; the replacement is converted to it.
            return Node("null_replacement", span, first.type, children=[_as(first, "argument"), _as(second, "argument")])
        raise Opaque(e, f"{name} is not lowered yet")

    def require_comparable(self, e, first: Node, second: Node) -> None:
        for t in (first.type, second.type):
            if t is None or t.name not in ("integer", "money", "boolean"):
                raise Opaque(e, f"comparing or replacing values of type {t} is not lowered yet")

    def predicate(self, e) -> Node:
        kind = type_name(e)
        span = self.parsed.span(e)
        if kind == "BooleanParenthesisExpression":
            return self.predicate(e.Expression)
        if kind == "BooleanBinaryExpression":
            op = {"And": "and", "Or": "or"}[str(e.BinaryExpressionType)]
            return Node(op, span, VType.of("boolean"), children=[
                _as(self.predicate(e.FirstExpression), "argument"), _as(self.predicate(e.SecondExpression), "argument")])
        if kind == "BooleanComparisonExpression":
            operator = COMPARISONS.get(str(e.ComparisonType))
            if operator is None:
                raise Opaque(e, f"{e.ComparisonType} is not lowered yet")
            first, second = self.expression(e.FirstExpression), self.expression(e.SecondExpression)
            self.require_comparable(e, first, second)
            return Node("compare", span, VType.of("boolean"), {"operator": operator, "rule": None},
                        [_as(first, "argument"), _as(second, "argument")])
        if kind == "BooleanIsNullExpression":
            node = Node("is_null", span, VType.of("boolean"), children=[_as(self.expression(e.Expression), "argument")])
            return Node("not", span, VType.of("boolean"), children=[_as(node, "argument")]) if e.IsNot else node
        raise Opaque(e, f"{kind} is not lowered yet")


def _as(node: Node, role: str) -> Node:
    node.role = role
    return node


def _walk(fragment, visit) -> None:
    """Visit a ScriptDom fragment and every fragment below it."""
    import Microsoft.SqlServer.TransactSql.ScriptDom as scriptdom  # pyright: ignore[reportMissingImports]  (a .NET namespace, loaded by pythonnet at run time)

    class Visitor(scriptdom.TSqlFragmentVisitor):
        __namespace__ = "migrt.lowering"

        def Visit(self, node):  # noqa: N802 (ScriptDom's name)
            visit(node)

    fragment.Accept(Visitor())


def lower_view(reader: CatalogReader, identity: str, parsed: Parsed) -> Artefact:
    return ViewLowering(reader, identity, parsed).lower()
