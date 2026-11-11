"""Lowering of T-SQL expressions into the operation vocabulary (spec sections 4 and 5).

Every expression gets its T-SQL type from the catalog, the operation that means exactly what
the T-SQL means (an integer division is `integer_divide`, `+` on strings is
`concat_null_propagating`), and the conversions SQL Server's data type precedence makes, written
out as `implicit_convert`. Every string comparison names its comparison rule, taken from collation
precedence (collations.py). What cannot be expressed raises `Opaque`, caught by the nearest
caller that can keep the source verbatim.

Conventions of the nodes (the intermediate representation's format version is in core/ir.py):
operands are children with the role `argument`, in order; `compare` has `operator` and `rule`;
`between`, `in_list`, `like`, `null_if_equal`, `minimum` and `maximum` have `rule` when they compare
strings (None otherwise); a `column` names the `relation` it is read through, the catalog `table`
(None for a derived relation) and the `column`; a `variable` names the parameter.
"""
from __future__ import annotations

import dataclasses
import re

from ...core import secrets
from ...core.catalog import CatalogReader
from ...core.ir import UNKNOWN, Node, VType
from . import collations, names
from . import types as T
from .parser import Parsed, walk

AGGREGATES = {"COUNT", "COUNT_BIG", "SUM", "AVG", "MIN", "MAX", "STDEV", "STDEVP", "VAR", "VARP", "STRING_AGG",
              "GROUPING", "GROUPING_ID", "CHECKSUM_AGG", "APPROX_COUNT_DISTINCT"}
BINARY = {"Add": "add", "Subtract": "subtract", "Multiply": "multiply", "Divide": "divide", "Modulo": "modulo"}
COMPARISONS = {"Equals": "=", "NotEqualToBrackets": "<>", "NotEqualToExclamation": "<>", "GreaterThan": ">",
               "LessThan": "<", "GreaterThanOrEqualTo": ">=", "LessThanOrEqualTo": "<=",
               "NotLessThan": ">=", "NotGreaterThan": "<="}
DATE_PARTS = {"year": "year", "yy": "year", "yyyy": "year", "quarter": "quarter", "qq": "quarter", "q": "quarter",
              "month": "month", "mm": "month", "m": "month", "dayofyear": "dayofyear", "dy": "dayofyear",
              "y": "dayofyear", "day": "day", "dd": "day", "d": "day", "week": "week", "wk": "week", "ww": "week",
              "weekday": "weekday", "dw": "weekday", "hour": "hour", "hh": "hour", "minute": "minute", "mi": "minute",
              "n": "minute", "second": "second", "ss": "second", "s": "second", "millisecond": "millisecond",
              "ms": "millisecond", "microsecond": "microsecond", "mcs": "microsecond", "nanosecond": "nanosecond",
              "ns": "nanosecond", "iso_week": "iso_week", "isowk": "iso_week", "isoww": "iso_week",
              "tzoffset": "tzoffset", "tz": "tzoffset"}
NON_DETERMINISTIC = {"NEWID", "RAND", "NEWSEQUENTIALID"}
CONCAT_ASSUMPTION = ("CONCAT_NULL_YIELDS_NULL is assumed ON where + joins strings: the module does not store it, and "
                     "every current client driver sets it ON. With it OFF, a NULL part would count as an empty string.")


class Opaque(Exception):
    """Raised inside lowering when a construct is not handled; caught at the nearest point
    that can keep the source verbatim as an opaque fragment."""

    def __init__(self, fragment, reason: str):
        self.fragment, self.reason = fragment, reason
        super().__init__(reason)


@dataclasses.dataclass
class Relation:
    """A named row source in scope: a table, view, function result, derived table, named query or the rows of a
    shredded XML value. `columns` is None where the catalog cannot say what it holds (an external reference)."""
    alias: str
    columns: dict[str, VType] | None
    table: str | None = None


def type_name(fragment) -> str:
    return str(fragment.GetType().Name)


def as_role(node: Node, role: str) -> Node:
    node.role = role
    return node


class Expressions:
    def __init__(self, reader: CatalogReader, identity: str, parsed: Parsed):
        self.reader, self.identity, self.parsed = reader, identity, parsed
        if identity.startswith("batch:"):            # a script file: its database is a fact, its schema dbo
            self.database, schema = str(reader.value(identity, "database")), "dbo"
        else:
            self.database, schema, _name = identity.split(".")
        collation = reader.value(self.database, "collation")
        self.case_sensitive = names.identifiers_case_sensitive(collation)
        self.name_context = {"database": self.database, "schemas": [schema, "dbo"],
                             "case_sensitive": self.case_sensitive}
        self.default_label = collations.Label(collation, collations.DEFAULT)
        # Marked secrets never enter the representation: text copied from the source is masked.
        self.secrets = [secrets.SecretSpan.from_json(s) for s in reader.value(identity, "secrets")
                        if s["fact"] == "source_text"]
        self.settings: set[str] = set()
        self.assumptions: set[str] = set()
        self.scopes: list[dict[str, Relation]] = []      # the relations of each enclosing query, innermost last
        self.variables: dict[str, tuple[str, VType]] = {}     # parameters and declared variables, by folded name
        self.row_sets: dict[str, tuple[str, dict[str, VType]]] = {}       # table variables
        self.temp_tables: dict[str, tuple[str, dict[str, VType]]] = {}   # temp tables that may exist here
        self.xml_namespaces: list[str] = []

    # ------------------------------------------------------------------ helpers

    def fold(self, name: str) -> str:
        return name if self.case_sensitive else name.casefold()

    def source(self, offset: int, length: int) -> str:
        """Source text, masked: each marked span becomes its named setting."""
        text, used = secrets.mask(self.parsed.text[offset:offset + length], self.secrets, prefix=self.identity,
                                  offset=offset)
        self.settings.update(used)
        return text

    def text_of(self, fragment) -> str:
        return self.source(int(fragment.StartOffset), int(fragment.FragmentLength))

    def span(self, fragment):
        return self.parsed.span(fragment)

    def node(self, kind: str, fragment, type_: VType | None = None, attrs: dict | None = None,
             children: list[Node] | None = None) -> Node:
        return Node(kind, self.span(fragment), type_, attrs or {}, list(children or []))

    def opaque(self, fragment, reason: str, role: str = "", *, barrier: bool = False) -> Node:
        reads: set[str] = set()
        hints: list[str] = []
        seen: set[int] = set()       # a hint is one node, though a node can be visited as two kinds

        def visit(f):
            kind = type_name(f)
            if kind.endswith(("TableHint", "OptimizerHint")) and int(f.StartOffset) not in seen:
                seen.add(int(f.StartOffset))
                hints.append(self.query_hint(f) if kind.endswith("OptimizerHint") else self.text_of(f))
            if kind == "ColumnReferenceExpression" and str(f.ColumnType) == "Regular":
                reads.add(".".join(str(i.Value) for i in f.MultiPartIdentifier.Identifiers))
            elif kind == "VariableReference":
                reads.add(str(f.Name))
            elif kind == "NamedTableReference" and f.SchemaObject is not None:
                reads.add(".".join(str(i.Value) for i in f.SchemaObject.Identifiers))
        walk(fragment, visit)
        attrs: dict = {"reason": reason, "text": self.text_of(fragment), "scope_barrier": barrier,
                       "references": sorted(reads)}
        if hints:
            attrs["hints"] = hints        # kept for the report, though the fragment is not lowered
        return Node("opaque_fragment", self.span(fragment), UNKNOWN, attrs, role=role)

    def query_hint(self, hint) -> str:
        """A query hint as written: a hint with a number carries its keyword apart from the number."""
        if type_name(hint) == "LiteralOptimizerHint":
            return f"{str(hint.HintKind).upper()} {self.text_of(hint.Value)}"
        return self.text_of(hint)

    def query_expression(self, qe):          # supplied by the query lowering
        raise NotImplementedError

    def contains_aggregate(self, fragment) -> bool:
        """Whether an aggregate function is called, other than as a window function and other than inside a subquery.
        An aggregate among a window function's arguments or ordering counts: it is a call of its own."""
        skip: list[tuple[int, int]] = []
        calls = []

        def visit(f):
            kind = type_name(f)
            if kind in ("ScalarSubquery", "QueryDerivedTable"):
                skip.append((int(f.StartOffset), int(f.StartOffset) + int(f.FragmentLength)))
            elif kind == "FunctionCall" and f.CallTarget is None and f.OverClause is None \
                    and str(f.FunctionName.Value).upper() in AGGREGATES:
                calls.append(int(f.StartOffset))
        walk(fragment, visit)
        return any(not any(a <= offset < b for a, b in skip) for offset in calls)

    # ------------------------------------------------------------------ names in scope

    def relation_of(self, alias: str) -> Relation | None:
        for frame in reversed(self.scopes):
            if self.fold(alias) in frame:
                return frame[self.fold(alias)]
        return None

    def find_column(self, e, parts: list[str]) -> tuple[Relation, str, VType]:
        if len(parts) == 2:
            relation = self.relation_of(parts[0])
            if relation is None:
                raise Opaque(e, f"{parts[0]} is not a table or alias in scope")
            candidates = [relation]
        elif len(parts) == 1:
            candidates = []
            for frame in reversed(self.scopes):
                candidates = list(frame.values())
                if any(r.columns is None or any(self.fold(c) == self.fold(parts[0]) for c in r.columns)
                       for r in candidates):
                    break
            else:
                candidates = []
        else:
            raise Opaque(e, "a column named with more than two parts is not lowered yet")
        found = []
        for relation in candidates:
            if relation.columns is None:
                raise Opaque(e, f"the columns of {relation.alias}, an external reference, are not known")
            found += [(relation, name, t) for name, t in relation.columns.items() if self.fold(name) == self.fold(parts[-1])]
        if len(found) != 1:
            raise Opaque(e, f"column {'.'.join(parts)} does not resolve to exactly one column in scope")
        return found[0]

    # ------------------------------------------------------------------ typing helpers

    def convert_toward(self, node: Node, target: VType) -> Node:
        """Wrap `node` in the implicit conversion SQL Server makes when its type meets `target`, if it makes one."""
        source = node.value_type
        if node.kind == "null":
            node.type = target
            return node
        if not T.needs_conversion(source, target):
            return node
        if target.name == "decimal" and source.name in ("integer", "money"):
            target = T.as_decimal(source)  # type: ignore[assignment]
        elif target.name == "string" and source.name == "string":
            target = T.string(source.length(), unit=target.str_param("unit"), fixed=source.flag("fixed"),
                              label=T.label_of(source))
        return Node("implicit_convert", node.span, target, {}, [as_role(node, "argument")])

    def check_comparable(self, e, a: VType, b: VType) -> None:
        if a.name in ("unknown", "unsupported_type") or b.name in ("unknown", "unsupported_type"):
            raise Opaque(e, f"comparing values of type {a} and {b} is not lowered yet")
        if a.name == b.name:
            if a.name == "xml":
                raise Opaque(e, "xml values cannot be compared")
            return
        simple = {"integer", "decimal", "money", "float", "boolean"}
        temporal = {"date", "local_timestamp", "timestamp_with_offset", "time_of_day"}
        names_ = {a.name, b.name}
        if names_ <= simple or names_ <= temporal:
            return
        if "string" in names_ and (names_ - {"string"}) <= simple | temporal | {"guid"}:
            return
        raise Opaque(e, f"comparing values of type {a} and {b} is not lowered yet")

    def meet(self, e, *nodes: Node) -> list[Node]:
        """The nodes with the conversions that bring them to the highest-precedence type among them."""
        typed = [n for n in nodes if n.kind != "null"]
        if not typed:
            for n in nodes:
                n.type = T.INT
            return list(nodes)
        top = typed[0].value_type
        for n in typed[1:]:
            self.check_comparable(e, top, n.value_type)
            top = T.unified(top, n.value_type)
        return [self.convert_toward(n, top) for n in nodes]

    def rule_for(self, e, *nodes: Node) -> dict | None:
        """The comparison rule of strings compared together, by collation precedence; None if none is a string."""
        return self.rule_of_types(e, *[n.value_type for n in nodes if T.is_string(n.type)])

    def ruled(self, e, node: Node) -> Node:
        """A key that orders, groups or partitions rows names the rule its strings are compared by."""
        if T.is_string(node.type):
            node.attrs = dict(node.attrs, rule=self.rule_for(e, node))
        return node

    def rule_of_types(self, e, *strings: VType) -> dict | None:
        if not strings:
            return None
        labels = []
        for t in strings:
            label = T.label_of(t)
            if label is None:
                raise Opaque(e, "the catalog holds no collation for a string being compared, so no comparison rule "
                                "can be named")
            labels.append(label)
        try:
            label = labels[0]
            for other in labels[1:]:
                label = collations.combine(label, other)
        except collations.CollationConflict as conflict:
            raise Opaque(e, f"{conflict}: a collation conflict") from None
        return collations.rule(label)

    # ------------------------------------------------------------------ expressions

    def expression(self, e) -> Node:
        kind = type_name(e)
        method = getattr(self, "x_" + kind, None)
        if method is None:
            raise Opaque(e, f"{kind} is not lowered yet")
        node = method(e)
        collation = getattr(e, "Collation", None)
        return node if collation is None else self.collated(e, node, str(collation.Value))

    def collated(self, e, node: Node, collation: str) -> Node:
        """`x COLLATE name`: the string keeps its value and takes the named collation, which beats every other."""
        if not T.is_string(node.type):
            raise Opaque(e, "COLLATE on a value that is not a string is not lowered yet")
        node.type = T.relabelled(node.value_type, collations.Label(collation, collations.EXPLICIT))
        node.attrs = dict(node.attrs, collate=collation)
        return node

    def x_ParenthesisExpression(self, e) -> Node:
        return self.expression(e.Expression)

    def x_ColumnReferenceExpression(self, e) -> Node:
        if str(e.ColumnType) != "Regular":
            raise Opaque(e, f"{e.ColumnType} column reference is not lowered yet")
        return self.column(e, [str(i.Value) for i in e.MultiPartIdentifier.Identifiers])

    def column(self, e, parts: list[str]) -> Node:
        relation, column, t = self.find_column(e, parts)
        return self.node("column", e, t, {"relation": relation.alias, "table": relation.table, "column": column})

    def x_VariableReference(self, e) -> Node:
        name = str(e.Name)
        found = self.variables.get(name.casefold())
        if found is None:
            if name.casefold() in self.row_sets:
                raise Opaque(e, f"{name} is a table variable, not a value")
            raise Opaque(e, f"{name} is not a parameter or a variable declared before this point in its batch: it may "
                            "have been declared by a statement kept as written, or in another batch")
        return self.node("variable", e, found[1], {"name": found[0]})

    def literal(self, e, type_: VType, value) -> Node:
        """A constant. One inside a marked secret lowers to its named setting, never to the literal (spec section 3)."""
        node = self.node("literal", e, type_, {"value": value})
        start = int(e.StartOffset)
        overlapping = secrets.mask(self.parsed.text[start:start + int(e.FragmentLength)], self.secrets,
                                   prefix=self.identity, offset=start)[1]
        if overlapping:
            self.settings.update(overlapping)
            node.attrs = {"secret_setting": overlapping[0]}
            if len(overlapping) > 1:
                node.attrs["secret_settings"] = overlapping
        return node

    def x_IntegerLiteral(self, e) -> Node:
        value = int(str(e.Value))
        if -2**31 <= value < 2**31:
            return self.literal(e, T.INT, value)
        return self.literal(e, T.decimal(len(str(abs(value))), 0), value)

    def x_NumericLiteral(self, e) -> Node:
        text = str(e.Value)
        whole, _, fraction = text.partition(".")
        precision = max(len((whole + fraction).lstrip("0")), len(fraction), 1)
        return self.literal(e, T.decimal(precision, len(fraction)), text)

    def x_RealLiteral(self, e) -> Node:
        return self.literal(e, T.FLOAT, str(e.Value))

    def x_MoneyLiteral(self, e) -> Node:
        return self.literal(e, T.money(19), str(e.Value))

    def x_BinaryLiteral(self, e) -> Node:
        text = str(e.Value)
        return self.literal(e, VType.of("binary", length=max((len(text) - 2 + 1) // 2, 1), fixed=False), text)

    def x_NullLiteral(self, e) -> Node:
        return self.node("null", e, UNKNOWN)

    def x_StringLiteral(self, e) -> Node:
        value = str(e.Value)
        national = bool(e.IsNational)
        cap = T.MAX_UTF16 if national else T.MAX_BYTES
        length = max(len(value), 1) if len(value) <= cap else T.UNLIMITED
        return self.literal(e, T.varchar(length, self.default_label, national), value)

    def x_UnaryExpression(self, e) -> Node:
        kind = str(e.UnaryExpressionType)
        operand = self.expression(e.Expression)
        if kind == "Positive":
            return operand
        if kind != "Negative":
            raise Opaque(e, f"unary {kind} is not lowered yet")
        t = operand.value_type
        if not T.is_numeric(t):
            raise Opaque(e, f"negating {t} is not lowered yet")
        return self.node("negate", e, t, children=[as_role(operand, "argument")])

    def x_BinaryExpression(self, e) -> Node:
        op = BINARY.get(str(e.BinaryExpressionType))
        if op is None:
            raise Opaque(e, f"{e.BinaryExpressionType} is not lowered yet")
        return self.arithmetic(e, op, self.expression(e.FirstExpression), self.expression(e.SecondExpression))

    def arithmetic(self, e, op: str, left: Node, right: Node) -> Node:
        """`left op right` for already lowered operands: the operation T-SQL's types make it (also `SET @x += y`)."""
        if op == "add" and T.is_string(left.type) and T.is_string(right.type):
            return self.concatenate(e, left, right)
        if left.kind == "null" and right.kind == "null":
            raise Opaque(e, "arithmetic on two NULL literals is not lowered yet")
        left, right = self.meet(e, left, right)
        a, b = left.value_type, right.value_type
        if a.name == "string":
            raise Opaque(e, f"{op} on strings is not lowered yet")
        if T.is_temporal(a) or a.name == "boolean":
            raise Opaque(e, f"{op} on {a} is not lowered yet: use the date functions")
        if a.name != b.name:
            raise Opaque(e, f"{op} on {a} and {b} is not lowered yet")
        found = T.arithmetic(op, a, b)
        if found is None:
            raise Opaque(e, f"{op} on {a} and {b} is not lowered yet")
        operation, result = found
        return self.node(operation, e, result, children=[as_role(left, "argument"), as_role(right, "argument")])

    def concatenate(self, e, left: Node, right: Node) -> Node:
        # A module does not store CONCAT_NULL_YIELDS_NULL: it follows the calling session, and every current client
        # driver sets it ON (the database option is overridden by the client). Lowering assumes ON and says so.
        self.assumptions.add(CONCAT_ASSUMPTION)
        parts = [left, right]
        flat: list[Node] = []
        for p in parts:
            flat += p.children_with("argument") if p.kind == "concat_null_propagating" else [p]
        result = flat[0].value_type
        try:
            for p in flat[1:]:
                result = T.concatenated(result, p.value_type)
        except collations.CollationConflict as conflict:
            raise Opaque(e, f"{conflict}: a collation conflict") from None
        flat = [self.convert_toward(p, result) if p.value_type.param("unit") != result.param("unit") else p
                for p in flat]
        return self.node("concat_null_propagating", e, result, children=[as_role(p, "argument") for p in flat])

    # ------------------------------------------------------------------ CASE, COALESCE and friends

    def x_SearchedCaseExpression(self, e) -> Node:
        whens = [(self.predicate(w.WhenExpression), self.expression(w.ThenExpression)) for w in e.WhenClauses]
        other = self.expression(e.ElseExpression) if e.ElseExpression is not None else None
        return self.case_node(e, whens, other)

    def x_SimpleCaseExpression(self, e) -> Node:
        if self.contains_call(e.InputExpression, NON_DETERMINISTIC):
            raise Opaque(e, "a simple CASE over a non-deterministic input is not lowered yet")
        whens = []
        for w in e.WhenClauses:
            first, second = self.meet(e, self.expression(e.InputExpression), self.expression(w.WhenExpression))
            rule = self.rule_for(e, first, second)
            whens.append((Node("compare", self.span(w.WhenExpression), T.BOOLEAN, {"operator": "=", "rule": rule},
                               [as_role(first, "argument"), as_role(second, "argument")]),
                          self.expression(w.ThenExpression)))
        other = self.expression(e.ElseExpression) if e.ElseExpression is not None else None
        return self.case_node(e, whens, other)

    def case_node(self, e, whens: list[tuple[Node, Node]], other: Node | None) -> Node:
        values = [t for _, t in whens] + ([other] if other is not None else [])
        converted = self.meet(e, *values)
        children: list[Node] = []
        for (condition, _), value in zip(whens, converted):
            children += [as_role(condition, "when"), as_role(value, "then")]
        if other is not None:
            children.append(as_role(converted[-1], "else"))
        result = self.common_type(converted)
        return self.node("case", e, result, children=children)

    def common_type(self, nodes: list[Node]) -> VType:
        """The type of whichever of the nodes is taken: the highest-precedence one, wide enough for all."""
        typed = [n.value_type for n in nodes if n.kind != "null"]
        result = typed[0] if typed else T.INT
        for t in typed[1:]:
            result = T.unified(result, t)
        return result

    def contains_call(self, fragment, names_: set[str]) -> bool:
        found = []

        def visit(f):
            if type_name(f) == "FunctionCall" and str(f.FunctionName.Value).upper() in names_:
                found.append(f)
        walk(fragment, visit)
        return bool(found)

    def x_IIfCall(self, e) -> Node:
        return self.case_node(e, [(self.predicate(e.Predicate), self.expression(e.ThenExpression))],
                              self.expression(e.ElseExpression))

    def x_CoalesceExpression(self, e) -> Node:
        converted = self.meet(e, *[self.expression(x) for x in e.Expressions])
        result = self.common_type(converted)
        return self.node("first_non_null", e, result, children=[as_role(c, "argument") for c in converted])

    def x_NullIfExpression(self, e) -> Node:
        own = self.expression(e.FirstExpression)
        result = own.value_type if own.kind != "null" else T.INT
        first, second = self.meet(e, own, self.expression(e.SecondExpression))
        return self.node("null_if_equal", e, result, {"rule": self.rule_for(e, first, second)},
                         [as_role(first, "argument"), as_role(second, "argument")])

    # ------------------------------------------------------------------ conversions

    def data_type(self, reference, *, converting: bool = False, label: collations.Label | None = None) -> VType:
        kind = type_name(reference)
        if kind == "XmlDataTypeReference":
            return VType.of("xml")
        if kind == "UserDataTypeReference" and reference.Name.SchemaIdentifier is None:
            base = str(reference.Name.BaseIdentifier.Value).casefold()
            if base == "sysname":                  # nvarchar(128), not null
                return T.declared("nvarchar", [128], label or self.default_label)
            if base in ("geography", "geometry", "hierarchyid"):
                return VType.of("unsupported_type", source_name=base)
        if kind != "SqlDataTypeReference":
            raise Opaque(reference, f"the type {self.text_of(reference)} is not lowered yet")
        arguments = [("max" if type_name(p) == "MaxLiteral" else int(str(p.Value))) for p in reference.Parameters]
        return T.declared(str(reference.SqlDataTypeOption), arguments, label or self.default_label,
                          converting=converting)

    def convert_node(self, e, kind: str, target: VType, operand: Node, style: Node | None) -> Node:
        if T.is_string(target) and T.is_string(operand.type):
            target = T.relabelled(target, T.label_of(operand.value_type))
        attrs: dict = {}
        children = [as_role(operand, "argument")]
        if style is not None and not (style.kind == "literal" and style.attrs.get("value") == 0):
            if kind == "convert":
                kind = "convert_with_style"
            if style.kind != "literal" or "value" not in style.attrs:
                raise Opaque(e, "a CONVERT style that is not a constant is not lowered yet")
            attrs["style"] = style.attrs["value"]
        attrs["target"] = target.to_json()
        return self.node(kind, e, target, attrs, children)

    def x_CastCall(self, e) -> Node:
        return self.convert_node(e, "convert", self.data_type(e.DataType, converting=True),
                                 self.expression(e.Parameter), None)

    def x_TryCastCall(self, e) -> Node:
        return self.convert_node(e, "try_convert", self.data_type(e.DataType, converting=True),
                                 self.expression(e.Parameter), None)

    def x_ConvertCall(self, e) -> Node:
        style = self.expression(e.Style) if e.Style is not None else None
        return self.convert_node(e, "convert", self.data_type(e.DataType, converting=True),
                                 self.expression(e.Parameter), style)

    def x_TryConvertCall(self, e) -> Node:
        style = self.expression(e.Style) if e.Style is not None else None
        return self.convert_node(e, "try_convert", self.data_type(e.DataType, converting=True),
                                 self.expression(e.Parameter), style)

    # ------------------------------------------------------------------ subqueries

    def subquery(self, e, qe) -> tuple[Node, list[tuple[str, VType]]]:
        return self.query_expression(qe)

    def x_ScalarSubquery(self, e) -> Node:
        plan, outputs = self.subquery(e, e.QueryExpression)
        if len(outputs) != 1:
            raise Opaque(e, "a scalar subquery must return one column")
        return self.node("scalar_query", e, outputs[0][1], children=[as_role(plan, "query")])

    # ------------------------------------------------------------------ predicates

    def predicate(self, e) -> Node:
        kind = type_name(e)
        method = getattr(self, "p_" + kind, None)
        if method is None:
            raise Opaque(e, f"{kind} is not lowered yet")
        return method(e)

    def boolean(self, kind: str, e, children: list[Node], attrs: dict | None = None) -> Node:
        return self.node(kind, e, T.BOOLEAN, attrs, [as_role(c, "argument") for c in children])

    def p_BooleanParenthesisExpression(self, e) -> Node:
        return self.predicate(e.Expression)

    def p_BooleanBinaryExpression(self, e) -> Node:
        op = {"And": "and", "Or": "or"}[str(e.BinaryExpressionType)]
        return self.boolean(op, e, [self.predicate(e.FirstExpression), self.predicate(e.SecondExpression)])

    def p_BooleanNotExpression(self, e) -> Node:
        return self.boolean("not", e, [self.predicate(e.Expression)])

    def p_BooleanComparisonExpression(self, e) -> Node:
        operator = COMPARISONS.get(str(e.ComparisonType))
        if operator is None:
            raise Opaque(e, f"{e.ComparisonType} is not lowered yet")
        first, second = self.meet(e, self.expression(e.FirstExpression), self.expression(e.SecondExpression))
        return self.boolean("compare", e, [first, second], {"operator": operator, "rule": self.rule_for(e, first, second)})

    def p_BooleanIsNullExpression(self, e) -> Node:
        node = self.boolean("is_null", e, [self.expression(e.Expression)])
        return self.boolean("not", e, [node]) if e.IsNot else node

    def p_BooleanTernaryExpression(self, e) -> Node:
        kind = str(e.TernaryExpressionType)
        if kind not in ("Between", "NotBetween"):
            raise Opaque(e, f"{kind} is not lowered yet")
        value, low, high = self.meet(e, self.expression(e.FirstExpression), self.expression(e.SecondExpression),
                                     self.expression(e.ThirdExpression))
        node = self.boolean("between", e, [value, low, high], {"rule": self.rule_for(e, value, low, high)})
        return self.boolean("not", e, [node]) if kind == "NotBetween" else node

    def p_LikePredicate(self, e) -> Node:
        value, pattern = self.expression(e.FirstExpression), self.expression(e.SecondExpression)
        escape = self.expression(e.EscapeExpression) if e.EscapeExpression is not None else None
        operands = [value, pattern] + ([escape] if escape else [])
        if not all(T.is_string(n.type) for n in operands):
            raise Opaque(e, "LIKE over values that are not all strings is not lowered yet")
        rule = self.rule_for(e, *operands)
        # Unlike a comparison, LIKE does not pad: the spaces at the end of a pattern must match.
        node = self.boolean("like", e, operands, {"rule": dict(rule, ignore_trailing_spaces=False) if rule else rule})
        return self.boolean("not", e, [node]) if e.NotDefined else node

    def p_InPredicate(self, e) -> Node:
        value = self.expression(e.Expression)
        if e.Subquery is not None:
            plan, outputs = self.subquery(e, e.Subquery.QueryExpression)
            if len(outputs) != 1:
                raise Opaque(e, "IN over a subquery needs it to return one column")
            marker = Node("column", self.span(e), outputs[0][1], {"relation": "(subquery)", "table": None,
                                                                 "column": outputs[0][0]})
            value, converted = self.meet(e, value, marker)
            if converted is not marker:
                raise Opaque(e, "IN over a subquery whose column needs converting to the compared type is not lowered "
                                "yet")
            node = self.boolean("in_query", e, [value], {"rule": self.rule_for(e, value, marker)})
            node.children.append(as_role(plan, "query"))
        else:
            items = [self.expression(v) for v in e.Values]
            converted = self.meet(e, value, *items)
            node = self.boolean("in_list", e, [converted[0]], {"rule": self.rule_for(e, *converted)})
            node.children += [as_role(c, "item") for c in converted[1:]]
        return self.boolean("not", e, [node]) if e.NotDefined else node

    def p_ExistsPredicate(self, e) -> Node:
        plan, _ = self.subquery(e, e.Subquery.QueryExpression)
        return self.node("exists", e, T.BOOLEAN, children=[as_role(plan, "query")])

    # ------------------------------------------------------------------ function calls

    def x_LeftFunctionCall(self, e) -> Node:
        return self.cut(e, "left_part")

    def x_RightFunctionCall(self, e) -> Node:
        return self.cut(e, "right_part")

    def cut(self, e, operation: str) -> Node:
        params = [self.expression(p) for p in e.Parameters]
        if len(params) != 2 or not T.is_string(params[0].type):
            raise Opaque(e, f"{operation} needs a string and a count")
        text, count = params
        return self.node(operation, e, T.text_like(text.value_type),
                         children=[as_role(text, "argument"), as_role(count, "argument")])

    def x_FunctionCall(self, e) -> Node:
        name = str(e.FunctionName.Value).upper()
        if e.OverClause is not None:
            return self.window(e, name)
        if e.CallTarget is not None:
            return self.call_target(e, name)
        if e.WithinGroupClause is not None and getattr(e.WithinGroupClause, "OrderByClause", None) is not None:
            raise Opaque(e, "WITHIN GROUP is not lowered yet")
        method = getattr(self, "fn_" + name.lower(), None)
        if method is None:
            raise Opaque(e, f"{str(e.FunctionName.Value)} is not lowered yet")
        return method(e, list(e.Parameters))

    def args(self, e, params, low: int, high: int | None = None) -> list[Node]:
        high = low if high is None else high
        if not low <= len(params) <= high:
            raise Opaque(e, f"{e.FunctionName.Value} called with {len(params)} arguments is not lowered yet")
        return [self.expression(p) for p in params]

    def one(self, e, kind: str, type_: VType, nodes: list[Node], attrs: dict | None = None) -> Node:
        return self.node(kind, e, type_, attrs, [as_role(n, "argument") for n in nodes])

    def part_of(self, e, parameter) -> str:
        kind = type_name(parameter)
        if kind == "ColumnReferenceExpression":
            text = str(parameter.MultiPartIdentifier.Identifiers[-1].Value)
        elif kind == "IdentifierLiteral":
            text = str(parameter.Value)
        else:
            raise Opaque(e, "a date part that is not a keyword is not lowered yet")
        part = DATE_PARTS.get(text.lower())
        if part is None:
            raise Opaque(e, f"{text} is not a date part lowering knows")
        return part

    def as_date(self, node: Node) -> Node:
        if T.is_string(node.type):
            return self.convert_toward(node, VType.of("local_timestamp", rounding="1/300 second"))
        return node

    def big_if_unlimited(self, node: Node) -> VType:
        t = node.type
        unlimited = t is not None and t.name in ("string", "binary") and t.length() == T.UNLIMITED
        return T.BIGINT if unlimited else T.INT

    # -- aggregates

    def fn_count(self, e, params):
        distinct = str(e.UniqueRowFilter) == "Distinct"
        if len(params) != 1:
            raise Opaque(e, "COUNT needs one argument")
        p = params[0]
        if type_name(p) == "ColumnReferenceExpression" and str(p.ColumnType) == "Wildcard":
            return self.node("count_rows", e, T.INT)
        arg = self.expression(p)
        return self.node("count_values", e, T.INT, {"distinct": distinct,
                                                    "rule": self.rule_for(e, arg) if distinct else None},
                         [as_role(arg, "argument")])

    def aggregate_argument(self, e, params) -> Node:
        if str(e.UniqueRowFilter) == "Distinct":
            raise Opaque(e, f"{e.FunctionName.Value}(DISTINCT ...) is not lowered yet")
        return self.args(e, params, 1)[0]

    def fn_sum(self, e, params):
        arg = self.aggregate_argument(e, params)
        result = T.sum_type(arg.value_type)
        if result is None:
            raise Opaque(e, f"SUM over {arg.type} is not lowered yet")
        return self.one(e, "sum", result, [arg])

    def fn_avg(self, e, params):
        arg = self.aggregate_argument(e, params)
        result = T.average_type(arg.value_type)
        if result is None:
            raise Opaque(e, f"AVG over {arg.type} is not lowered yet")
        return self.one(e, "average", result, [arg])

    def extreme(self, e, params, kind):
        arg = self.aggregate_argument(e, params)
        if arg.value_type.name in ("xml", "guid", "unsupported_type", "unknown") or arg.value_type.name == "boolean":
            raise Opaque(e, f"{e.FunctionName.Value} over {arg.type} is not lowered yet")
        return self.one(e, kind, arg.value_type, [arg], {"rule": self.rule_for(e, arg)})

    def fn_min(self, e, params):
        return self.extreme(e, params, "minimum")

    def fn_max(self, e, params):
        return self.extreme(e, params, "maximum")

    def statistical(self, e, params, name):
        arg = self.aggregate_argument(e, params)
        if not T.is_numeric(arg.type):
            raise Opaque(e, f"{name} over {arg.type} is not lowered yet")
        return self.one(e, "statistical_aggregate", T.FLOAT, [arg], {"name": name})

    def fn_stdev(self, e, params):
        return self.statistical(e, params, "sample standard deviation")

    def fn_stdevp(self, e, params):
        return self.statistical(e, params, "population standard deviation")

    def fn_var(self, e, params):
        return self.statistical(e, params, "sample variance")

    def fn_varp(self, e, params):
        return self.statistical(e, params, "population variance")

    # -- null handling

    def fn_isnull(self, e, params):
        first, second = self.args(e, params, 2)
        # ISNULL takes the first argument's type; the replacement is converted to it.
        if first.kind == "null":
            first, second = self.meet(e, first, second)
        else:
            if second.kind != "null":
                self.check_comparable(e, first.value_type, second.value_type)
            second = self.convert_toward(second, first.value_type)
        return self.one(e, "null_replacement", first.value_type, [first, second])

    # -- strings

    def string_arg(self, e, node: Node) -> Node:
        if not T.is_string(node.type):
            raise Opaque(e, f"{e.FunctionName.Value} over {node.type} is not lowered yet")
        return node

    def fn_len(self, e, params):
        (text,) = self.args(e, params, 1)
        return self.one(e, "length_without_trailing_spaces", self.big_if_unlimited(self.string_arg(e, text)), [text])

    def fn_datalength(self, e, params):
        (value,) = self.args(e, params, 1)
        return self.one(e, "byte_length", self.big_if_unlimited(value), [value])

    def same_string(self, e, kind, params, count=1):
        nodes = self.args(e, params, count)
        text = self.string_arg(e, nodes[0])
        return self.one(e, kind, text.value_type, nodes, {})

    def fn_upper(self, e, params):
        return self.same_string(e, "upper", params)

    def fn_lower(self, e, params):
        return self.same_string(e, "lower", params)

    def trim(self, e, params, side):
        (text,) = self.args(e, params, 1)
        t = self.string_arg(e, text).value_type
        return self.one(e, "trim", T.text_like(t),
                        [text], {"side": side})

    def fn_ltrim(self, e, params):
        return self.trim(e, params, "left")

    def fn_rtrim(self, e, params):
        return self.trim(e, params, "right")

    def fn_trim(self, e, params):
        return self.trim(e, params, "both")

    def fn_substring(self, e, params):
        text, start, length = self.args(e, params, 3)
        t = self.string_arg(e, text).value_type
        return self.one(e, "substring", T.text_like(t),
                        [text, start, length])

    def fn_charindex(self, e, params):
        nodes = self.args(e, params, 2, 3)
        find, text = self.string_arg(e, nodes[0]), self.string_arg(e, nodes[1])
        return self.one(e, "position_of", self.big_if_unlimited(text), nodes,
                        {"rule": self.rule_for(e, find, text)})

    def fn_replace(self, e, params):
        text, find, replacement = self.args(e, params, 3)
        for n in (text, find, replacement):
            self.string_arg(e, n)
        t = text.value_type
        return self.one(e, "replace_text", T.text_like(t),
                        [text, find, replacement], {"rule": self.rule_for(e, text, find, replacement)})

    def fn_replicate(self, e, params):
        text, count = self.args(e, params, 2)
        t = self.string_arg(e, text).value_type
        national = t.param("unit") == "utf16_units"
        length = T.UNLIMITED if t.param("length") == T.UNLIMITED else (T.MAX_UTF16 if national else T.MAX_BYTES)
        return self.one(e, "repeat_text", T.varchar(length, T.label_of(t), national), [text, count])

    def fn_stuff(self, e, params):
        text, start, length, replacement = self.args(e, params, 4)
        t = self.string_arg(e, text).value_type
        self.string_arg(e, replacement)
        return self.one(e, "overlay", T.varchar(T.UNLIMITED, T.label_of(t), t.param("unit") == "utf16_units"),
                        [text, start, length, replacement])

    def fn_concat(self, e, params):
        nodes = self.args(e, params, 2, 254)
        national = any(T.is_string(n.type) and n.value_type.param("unit") == "utf16_units" for n in nodes)
        labels = [T.label_of(n.value_type) for n in nodes if T.is_string(n.type)]
        label = None
        if labels and all(labels):
            label = labels[0]
            for other in labels[1:]:
                try:
                    label = collations.combine(label, other)  # type: ignore[arg-type]
                except collations.CollationConflict as conflict:
                    raise Opaque(e, f"{conflict}: a collation conflict") from None
        lengths = [n.value_type.length() for n in nodes if T.is_string(n.type)]
        cap = T.MAX_UTF16 if national else T.MAX_BYTES
        numbers = [n for n in lengths if isinstance(n, int)]
        if len(numbers) != len(lengths):
            length: int | str = T.UNLIMITED
        elif len(lengths) != len(nodes):
            length = cap          # a value that is not a string converts to text of a length the cap covers
        else:
            length = min(sum(numbers), cap)
        return self.one(e, "concat_null_as_empty", T.varchar(length, label, national), nodes)

    # -- dates

    def fn_year(self, e, params):
        return self.date_part(e, "year", self.args(e, params, 1)[0])

    def fn_month(self, e, params):
        return self.date_part(e, "month", self.args(e, params, 1)[0])

    def fn_day(self, e, params):
        return self.date_part(e, "day", self.args(e, params, 1)[0])

    def date_part(self, e, part: str, value: Node):
        return self.one(e, "date_part", T.INT, [self.as_date(value)], {"part": part})

    def fn_datepart(self, e, params):
        if len(params) != 2:
            raise Opaque(e, "DATEPART needs a part and a date")
        return self.date_part(e, self.part_of(e, params[0]), self.expression(params[1]))

    def fn_datename(self, e, params):
        if len(params) != 2:
            raise Opaque(e, "DATENAME needs a part and a date")
        value = self.as_date(self.expression(params[1]))
        return self.one(e, "date_name", T.varchar(30, self.default_label, True), [value],
                        {"part": self.part_of(e, params[0])})

    def fn_dateadd(self, e, params):
        if len(params) != 3:
            raise Opaque(e, "DATEADD needs a part, a number and a date")
        number, value = self.expression(params[1]), self.as_date(self.expression(params[2]))
        return self.one(e, "date_add", value.value_type, [number, value], {"part": self.part_of(e, params[0])})

    def date_diff(self, e, params, result):
        if len(params) != 3:
            raise Opaque(e, "DATEDIFF needs a part and two dates")
        start, end = self.as_date(self.expression(params[1])), self.as_date(self.expression(params[2]))
        return self.one(e, "date_diff_boundaries", result, [start, end], {"part": self.part_of(e, params[0])})

    def fn_datediff(self, e, params):
        return self.date_diff(e, params, T.INT)

    def fn_datediff_big(self, e, params):
        return self.date_diff(e, params, T.BIGINT)

    def current(self, e, params, kind, type_):
        self.args(e, params, 0)
        return self.node("current_time", e, type_, {"kind": kind})

    def fn_getdate(self, e, params):
        return self.current(e, params, "local, 1/300 second", VType.of("local_timestamp", rounding="1/300 second"))

    def fn_getutcdate(self, e, params):
        return self.current(e, params, "utc, 1/300 second", VType.of("local_timestamp", rounding="1/300 second"))

    def fn_sysdatetime(self, e, params):
        return self.current(e, params, "local, 100 nanoseconds", VType.of("local_timestamp", rounding="7 digits"))

    def fn_sysutcdatetime(self, e, params):
        return self.current(e, params, "utc, 100 nanoseconds", VType.of("local_timestamp", rounding="7 digits"))

    def fn_eomonth(self, e, params):
        nodes = self.args(e, params, 1, 2)
        return self.one(e, "end_of_month", VType.of("date"), [self.as_date(nodes[0])] + nodes[1:])

    def fn_isdate(self, e, params):
        (text,) = self.args(e, params, 1)
        return self.one(e, "is_date_text", T.INT, [text])

    # -- numbers

    def numeric_argument(self, e, node: Node) -> Node:
        if not T.is_numeric(node.type):
            raise Opaque(e, f"{e.FunctionName.Value} over {node.type} is not lowered yet")
        return node

    def numeric(self, e, params, name, count=1, result=None):
        nodes = self.args(e, params, count)
        first = self.numeric_argument(e, nodes[0])
        return self.one(e, "numeric_function", result or first.value_type, nodes, {"name": name})

    def fn_abs(self, e, params):
        return self.numeric(e, params, "absolute value")

    def fn_sign(self, e, params):
        return self.numeric(e, params, "sign")

    def whole(self, e, params, name):
        (value,) = self.args(e, params, 1)
        t = self.numeric_argument(e, value).value_type
        return self.one(e, "numeric_function", T.decimal(t.int_param("precision"), 0) if t.name == "decimal" else t,
                        [value], {"name": name})

    def fn_ceiling(self, e, params):
        return self.whole(e, params, "ceiling")

    def fn_floor(self, e, params):
        return self.whole(e, params, "floor")

    def fn_sqrt(self, e, params):
        return self.numeric(e, params, "square root", result=T.FLOAT)

    def fn_exp(self, e, params):
        return self.numeric(e, params, "exponential", result=T.FLOAT)

    def fn_log(self, e, params):
        return self.numeric(e, params, "natural logarithm", result=T.FLOAT)

    def fn_log10(self, e, params):
        return self.numeric(e, params, "base-10 logarithm", result=T.FLOAT)

    def fn_power(self, e, params):
        return self.numeric(e, params, "power", count=2)

    def fn_round(self, e, params):
        nodes = self.args(e, params, 2, 3)
        value = self.numeric_argument(e, nodes[0])
        if len(nodes) == 3 and (nodes[2].kind != "literal" or "value" not in nodes[2].attrs):
            raise Opaque(e, "ROUND with a function argument that is not a constant is not lowered yet")
        truncate = len(nodes) == 3 and nodes[2].attrs["value"] != 0
        return self.one(e, "round", value.value_type, nodes[:2], {"truncate": truncate})

    def fn_rand(self, e, params):
        self.args(e, params, 0, 1)
        return self.node("random_number", e, T.FLOAT)

    def fn_newid(self, e, params):
        self.args(e, params, 0)
        return self.node("new_guid", e, VType.of("guid"))

    def fn_isnumeric(self, e, params):
        (text,) = self.args(e, params, 1)
        return self.one(e, "is_numeric_text", T.INT, [text])

    # -- the session and security

    def fn_session_context(self, e, params):
        nodes = self.args(e, params, 1, 2)
        return self.one(e, "session_value", VType.of("unsupported_type", source_name="sql_variant"), nodes,
                        {"name": "SESSION_CONTEXT"})

    def fn_original_login(self, e, params):
        self.args(e, params, 0)
        return self.node("session_value", e, T.varchar(128, self.default_label, True), {"name": "ORIGINAL_LOGIN"})

    def fn_suser_sname(self, e, params):
        nodes = self.args(e, params, 0, 1)
        return self.one(e, "session_value", T.varchar(128, self.default_label, True), nodes, {"name": "SUSER_SNAME"})

    def fn_is_rolemember(self, e, params):
        nodes = self.args(e, params, 1, 2)
        return self.one(e, "role_membership", T.INT, nodes, {"name": "IS_ROLEMEMBER"})

    def fn_decompress(self, e, params):
        (value,) = self.args(e, params, 1)
        return self.one(e, "compress_bytes", VType.of("binary", length=T.UNLIMITED, fixed=False), [value],
                        {"direction": "decompress"})

    # ------------------------------------------------------------------ calls with a target

    def call_target(self, e, name: str) -> Node:
        target = e.CallTarget
        if type_name(target) != "MultiPartIdentifierCallTarget":
            raise Opaque(e, f"a call on {type_name(target)} is not lowered yet")
        qualifier = [str(i.Value) for i in target.MultiPartIdentifier.Identifiers]
        if name in ("VALUE", "QUERY", "EXIST"):
            return self.xml_method(e, name, qualifier)
        if len(qualifier) == 1:
            return self.user_function(e, (qualifier[0], str(e.FunctionName.Value)))
        raise Opaque(e, f"a call to {'.'.join(qualifier)}.{e.FunctionName.Value} is not lowered yet")

    def xml_method(self, e, name: str, qualifier: list[str]) -> Node:
        source = self.column(e, qualifier)
        if source.value_type.name != "xml":
            raise Opaque(e, f"{name.lower()}() on a column that is not xml is not lowered yet")
        params = list(e.Parameters)
        path = self.expression(params[0]) if params else None
        if path is None or path.kind != "literal" or "value" not in path.attrs:
            raise Opaque(e, "an XML method whose path is not a plain string literal is not lowered yet")
        attrs: dict = {"kind": name.lower(), "path": path.attrs["value"], "namespaces": list(self.xml_namespaces)}
        if name == "VALUE":
            if len(params) != 2 or type_name(params[1]) != "StringLiteral":
                raise Opaque(e, "XML value() needs a path and a type name")
            result = self.xml_type(e, str(params[1].Value))
        elif name == "EXIST":
            result = T.INT
        else:
            result = VType.of("xml")
        attrs["target"] = result.to_json()
        return self.node("xml_value", e, result, attrs, [as_role(source, "argument")])

    def xml_type(self, e, text: str) -> VType:
        match = re.fullmatch(r"\s*(\w+)\s*(?:\(\s*(\w+)\s*(?:,\s*(\d+)\s*)?\))?\s*", text)
        if not match:
            raise Opaque(e, f"the type {text!r} is not lowered yet")
        arguments = [("max" if a.lower() == "max" else int(a)) for a in match.groups()[1:] if a]
        return T.declared(match.group(1), arguments, self.default_label, converting=False)

    def user_function(self, e, parts: tuple[str, ...]) -> Node:
        context = dict(self.name_context, kinds={"scalar_function"})
        try:
            found = self.reader.resolve(parts, context)
        except names.AmbiguousName as ambiguous:
            raise Opaque(e, str(ambiguous)) from None
        if found is None:
            raise Opaque(e, f"{'.'.join(parts)} does not resolve to a scalar function in the catalog")
        returns = self.reader.value(found, "returns")
        nodes = self.function_arguments(e, found, list(e.Parameters))
        return self.node("call_scalar_function", e, T.from_column(returns, default=self.default_label),
                         {"function": found}, [as_role(n, "argument") for n in nodes])

    def function_arguments(self, e, function: str, params) -> list[Node]:
        declared = self.reader.value(function, "parameters")
        if len(params) != len(declared):
            raise Opaque(e, f"{function} is called with {len(params)} arguments for {len(declared)} parameters: "
                            "defaults are not lowered yet")
        out = []
        for p, parameter in zip(params, declared):
            if type_name(p) == "DefaultLiteral":
                raise Opaque(e, "DEFAULT as an argument is not lowered yet")
            out.append(self.convert_toward(self.expression(p), T.from_column(parameter, default=self.default_label)))
        return out


    # ------------------------------------------------------------------ windows

    def window(self, e, name: str) -> Node:
        if e.CallTarget is not None:
            raise Opaque(e, "a window function called on a target is not lowered yet")
        over = e.OverClause
        ranking = {"RANK": ("rank", {"dense": False}), "DENSE_RANK": ("rank", {"dense": True})}
        params = list(e.Parameters)
        if name in ranking:
            kind, attrs = ranking[name]
            function = self.node(kind, e, T.BIGINT, attrs)
        elif name == "ROW_NUMBER":
            function = self.node("row_number", e, T.BIGINT)
        elif name == "NTILE":
            function = self.one(e, "bucket_number", T.INT, self.args(e, params, 1))
        elif name in ("LAG", "LEAD"):
            nodes = self.args(e, params, 1, 3)
            function = self.one(e, "offset_value", nodes[0].value_type, nodes,
                                {"direction": "before" if name == "LAG" else "after"})
        elif name in ("FIRST_VALUE", "LAST_VALUE"):
            (value,) = self.args(e, params, 1)
            function = self.one(e, "frame_value", value.value_type, [value],
                                {"end": "first" if name == "FIRST_VALUE" else "last"})
        elif name in AGGREGATES:
            function = getattr(self, "fn_" + name.lower())(e, params)
        else:
            raise Opaque(e, f"{name} OVER (...) is not lowered yet")
        children = [as_role(function, "function")]
        children += [as_role(self.ruled(p, self.expression(p)), "partition") for p in over.Partitions]
        ordered = over.OrderByClause is not None
        if ordered:
            children += self.window_order(over.OrderByClause)
        frame = self.frame(e, over.WindowFrameClause, ordered)
        return self.node("window", e, function.type, {"frame": frame}, children)

    def window_order(self, clause) -> list[Node]:
        keys = []
        for element in clause.OrderByElements:
            key = self.ruled(element.Expression, self.expression(element.Expression))
            key.attrs = dict(key.attrs, direction="descending" if str(element.SortOrder) == "Descending" else "ascending")
            keys.append(as_role(key, "order"))
        return keys

    def frame(self, e, clause, ordered: bool) -> dict:
        if clause is None:
            if ordered:
                return {"unit": "range", "start": "unbounded_preceding", "end": "current_row"}
            return {"unit": "partition", "start": "unbounded_preceding", "end": "unbounded_following"}
        edge = {"UnboundedPreceding": "unbounded_preceding", "UnboundedFollowing": "unbounded_following",
                "CurrentRow": "current_row"}

        def bound(delimiter):
            kind = str(delimiter.WindowDelimiterType)
            if kind in edge:
                return edge[kind]
            if delimiter.OffsetValue is None or type_name(delimiter.OffsetValue) != "IntegerLiteral":
                raise Opaque(e, "a window frame offset that is not an integer literal is not lowered yet")
            return f"{delimiter.OffsetValue.Value} {'preceding' if kind.endswith('Preceding') else 'following'}"
        top = bound(clause.Top)
        bottom = bound(clause.Bottom) if clause.Bottom is not None else "current_row"
        return {"unit": str(clause.WindowFrameType).lower(), "start": top, "end": bottom}
