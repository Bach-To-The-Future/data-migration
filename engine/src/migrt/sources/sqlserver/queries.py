"""Lowering of T-SQL queries into the logical plan (spec section 4, item 3).

A query keeps the source's names (aliases, common table expression names) and its order of
steps: the row sources (`read_table`, `named_query`, `call_table_function`, `read_values`,
`shred_xml`, `pivot`, `external_reference`) joined by `join` and `lateral_join`, then
`filter` for WHERE, then one `project` or `aggregate` step holding the key expressions, the
HAVING condition (`having`) and the select list (`output`), then `distinct`, `sort`, and `limit` or
`skip_and_limit`. A set operation is `union`, `intersect` or `except` over `left` and `right`.
Common table expressions are `named_query` children of the outermost step, role `definition`.

Roles: a step's input is `input` (`left` and `right` for a join, a set operation); a join's
`condition`; a sort key is `order`, with a `direction`; a limit's `count` and a skip's `skip`.
Columns of a select list can be read by name in ORDER BY through the relation `(output)`.
Table, join and query hints are kept as `hints` and `query_hints` and listed on the artefact.
"""
from __future__ import annotations

from ...core.ir import UNKNOWN, Node, VType
from . import names
from . import types as T
from .expressions import Expressions, Opaque, Relation, as_role, type_name
from .parser import walk

JOINS = {"Inner": "inner", "LeftOuter": "left", "RightOuter": "right", "FullOuter": "full"}
OUTPUT = "(output)"
PIVOT_AGGREGATES = {"SUM": "sum", "AVG": "average", "MIN": "minimum", "MAX": "maximum", "COUNT": "count_values"}


class Queries(Expressions):
    def __init__(self, *args):
        super().__init__(*args)
        self.ctes: list[dict[str, Relation]] = []
        self.trigger_table: str | None = None       # set while lowering a trigger: `inserted` and `deleted` read it

    # ------------------------------------------------------------------ statements and query expressions

    def select_statement(self, select, *, into: bool = False) -> tuple[Node, list[tuple[str, VType]]]:
        """A SELECT statement with its common table expressions, XML namespaces and query hints. `into` lets the
        caller take the INTO clause (a temp table), which is otherwise refused."""
        with_ = select.WithCtesAndXmlNamespaces
        self.ctes.append({})
        namespaces = self.xml_namespaces
        definitions: list[Node] = []
        try:
            if with_ is not None:
                if with_.XmlNamespaces is not None:
                    self.xml_namespaces = [self.text_of(x) for x in with_.XmlNamespaces.XmlNamespacesElements]
                for cte in with_.CommonTableExpressions:
                    definitions.append(self.common_table_expression(cte))
            if select.Into is not None and not into:
                raise Opaque(select, "SELECT INTO creates a table: it is not lowered")
            plan, outputs = self.query_expression(select.QueryExpression)
        finally:
            self.ctes.pop()
            self.xml_namespaces = namespaces        # a statement's XML namespaces end with it
        plan.children += definitions
        if select.OptimizerHints is not None and select.OptimizerHints.Count:
            plan.attrs = dict(plan.attrs, query_hints=[self.query_hint(h) for h in select.OptimizerHints])
        return plan, outputs

    def common_table_expression(self, cte) -> Node:
        name = str(cte.ExpressionName.Value)
        found = []

        def visit(f):
            if type_name(f) == "NamedTableReference" and f.SchemaObject.SchemaIdentifier is None \
                    and self.fold(str(f.SchemaObject.BaseIdentifier.Value)) == self.fold(name):
                found.append(f)
        walk(cte.QueryExpression, visit)
        if found:
            raise Opaque(cte, f"the common table expression {name} refers to itself: recursive named queries are "
                              "not lowered yet")
        plan, outputs = self.query_expression(cte.QueryExpression)
        outputs = self.renamed(cte, plan, outputs, cte.Columns)
        self.ctes[-1][self.fold(name)] = Relation(name, dict(outputs), None)
        return Node("named_query", self.span(cte), None, {"name": name}, [as_role(plan, "query")], role="definition")

    def renamed(self, fragment, plan: Node | None, outputs, columns) -> list[tuple[str, VType]]:
        """The outputs under a column list written after a name (`AS t (a, b)`); a plan's output nodes take the names."""
        if columns is None or not columns.Count:
            return outputs
        if columns.Count != len(outputs):
            raise Opaque(fragment, "the column list names a different number of columns than the query returns")
        new_names = [str(c.Value) for c in columns]
        if plan is not None:
            self.rename_outputs(plan, new_names)
        return [(name, t) for name, (_, t) in zip(new_names, outputs)]

    def rename_outputs(self, plan: Node, new_names: list[str]) -> None:
        block = plan
        while block.kind not in ("project", "aggregate"):
            block = block.children_with("input" if block.children_with("input") else "left")[0]
        renamed = {}
        for output, new in zip(block.children_with("output"), new_names):
            renamed[output.attrs["output_name"]] = new
            output.attrs = dict(output.attrs, output_name=new)
        for node in plan.walk():
            if node.kind == "column" and node.attrs.get("relation") == OUTPUT:
                node.attrs = dict(node.attrs, column=renamed.get(node.attrs["column"], node.attrs["column"]))

    def query_expression(self, qe) -> tuple[Node, list[tuple[str, VType]]]:
        kind = type_name(qe)
        if kind == "QuerySpecification":
            return self.specification(qe)
        if kind == "QueryParenthesisExpression":
            plan, outputs = self.query_expression(qe.QueryExpression)
        elif kind == "BinaryQueryExpression":
            plan, outputs = self.set_operation(qe)
        else:
            raise Opaque(qe, f"{kind} is not lowered yet")
        aliases = {self.fold(name): (name, t) for name, t in outputs}
        return self.finish(qe, plan, outputs, lambda element: self.output_key(element.Expression, aliases, outputs,
                                                                                True)), outputs

    def set_operation(self, qe) -> tuple[Node, list[tuple[str, VType]]]:
        left, left_outputs = self.query_expression(qe.FirstQueryExpression)
        right, right_outputs = self.query_expression(qe.SecondQueryExpression)
        if len(left_outputs) != len(right_outputs):
            raise Opaque(qe, "the queries of a set operation return different numbers of columns")
        outputs, rules = [], []
        for (name, a), (_, b) in zip(left_outputs, right_outputs):
            if a.name in ("unknown",) or b.name in ("unknown",) or T.needs_conversion(a, b) or a.name == "xml":
                raise Opaque(qe, f"a set operation over {a} and {b} needs conversions: not lowered yet")
            outputs.append((name, T.unified(a, b)))
            rules.append(self.rule_of_types(qe, a, b) if T.is_string(a) else None)
        operation = str(qe.BinaryQueryExpressionType)
        kind = {"Union": "union", "Intersect": "intersect", "Except": "except"}.get(operation)
        if kind is None:
            raise Opaque(qe, f"{operation} is not lowered yet")
        attrs: dict = {"all": bool(qe.All)} if kind == "union" else {}
        if any(rules) and not (kind == "union" and qe.All):
            attrs["rules"] = rules
        return Node(kind, self.span(qe), None, attrs, [as_role(left, "left"), as_role(right, "right")]), outputs

    # ------------------------------------------------------------------ one query specification

    def specification(self, spec) -> tuple[Node, list[tuple[str, VType]]]:
        if spec.ForClause is not None and type_name(spec.ForClause) not in ("UpdateForClause", "ReadOnlyForClause"):
            raise Opaque(spec, "FOR XML is not lowered yet" if "Xml" in type_name(spec.ForClause)
                         else "FOR JSON is not lowered yet")      # FOR UPDATE and FOR READ ONLY are a cursor's (cursors.py)
        source = Node("read_values", self.span(spec), None, {"columns": [], "rows": 1, "alias": None})
        self.scopes.append({})
        try:
            if spec.FromClause is not None:
                refs = [self.table_reference(r) for r in spec.FromClause.TableReferences]
                source = refs[0]
                for extra in refs[1:]:
                    source = Node("join", self.span(spec.FromClause), None, {"kind": "cross"},
                                  [as_role(source, "left"), as_role(extra, "right")])
            if spec.WhereClause is not None:
                condition = self.predicate(spec.WhereClause.SearchCondition)
                source = Node("filter", self.span(spec.WhereClause), None, {},
                              [as_role(source, "input"), as_role(condition, "condition")])
            block, outputs = self.select_block(spec, source)
            aliases = {self.fold(name): (name, t) for name, t in outputs}
            plan = block
            if str(spec.UniqueRowFilter) == "Distinct":
                rules = [self.rule_of_types(spec, t) if T.is_string(t) else None for _, t in outputs]
                plan = Node("distinct", self.span(spec), None, {"rules": rules} if any(rules) else {},
                            [as_role(plan, "input")])
            plan = self.finish(spec, plan, outputs, lambda element: self.output_key(element.Expression, aliases,
                                                                                   outputs, False))
            if spec.TopRowFilter is not None:
                top = spec.TopRowFilter
                count = self.expression(top.Expression)
                plan = Node("limit", self.span(top), None, {"percent": bool(top.Percent), "with_ties": bool(top.WithTies)},
                            [as_role(plan, "input"), as_role(count, "count")])
        finally:
            self.scopes.pop()
        return plan, outputs

    def select_block(self, spec, source: Node) -> tuple[Node, list[tuple[str, VType]]]:
        grouping, having = spec.GroupByClause, spec.HavingClause
        items = list(spec.SelectElements)
        aggregating = grouping is not None or having is not None or any(
            self.contains_aggregate(i) for i in items) or (
            spec.OrderByClause is not None and self.contains_aggregate(spec.OrderByClause))
        children = [as_role(source, "input")]
        if grouping is not None:
            if str(grouping.GroupByOption) != "None" or grouping.All:
                raise Opaque(grouping, "GROUP BY options are not lowered yet")
            for specification in grouping.GroupingSpecifications:
                if type_name(specification) != "ExpressionGroupingSpecification":
                    raise Opaque(specification, f"{type_name(specification)} is not lowered yet")
                children.append(as_role(self.ruled(specification, self.expression(specification.Expression)), "key"))
        if having is not None:
            children.append(as_role(self.predicate(having.SearchCondition), "having"))
        outputs: list[tuple[str, VType]] = []
        for item in items:
            for node in self.select_item(item, len(outputs) + 1):
                children.append(node)
                outputs.append((node.attrs["output_name"], node.type or UNKNOWN))
        return Node("aggregate" if aggregating else "project", self.span(spec), None, {}, children), outputs

    def select_item(self, item, position: int) -> list[Node]:
        kind = type_name(item)
        if kind == "SelectStarExpression":
            return self.star(item)
        if kind == "SelectSetVariable":
            return [self.assigned_item(item)]
        if kind != "SelectScalarExpression":
            raise Opaque(item, f"{kind} is not lowered yet")
        expression = item.Expression
        if item.ColumnName is not None:
            name = str(item.ColumnName.Value)
        elif type_name(expression) == "ColumnReferenceExpression" and str(expression.ColumnType) == "Regular":
            name = str(list(expression.MultiPartIdentifier.Identifiers)[-1].Value)
        else:
            name = f"(column {position})"
        try:
            node = self.expression(expression)
        except Opaque as opaque:
            node = self.opaque(expression, opaque.reason)
        node.span = self.span(item)
        node.role = "output"
        node.attrs = dict(node.attrs, output_name=name)
        return [node]

    def assigned_item(self, item) -> Node:
        """`@v = expression` in a select list: an output named by the variable, converted to its type."""
        if str(item.AssignmentKind) != "Equals":
            raise Opaque(item, "a compound assignment in a SELECT list is not lowered yet")
        name = str(item.Variable.Name)
        found = self.variables.get(name.casefold())
        if found is None:
            raise Opaque(item, f"{name} is not a parameter or a variable declared before this point in its batch")
        try:
            node = self.convert_toward(self.expression(item.Expression), found[1])
        except Opaque as opaque:
            node = self.opaque(item.Expression, opaque.reason)
        node.span = self.span(item)
        node.role = "output"
        node.attrs = dict(node.attrs, output_name=found[0])
        return node

    def star(self, item) -> list[Node]:
        if item.Qualifier is not None:
            alias = str(item.Qualifier.Identifiers[-1].Value)
            relation = self.relation_of(alias)
            if relation is None:
                raise Opaque(item, f"{alias} is not a table or alias in scope")
            relations = [relation]
        else:
            relations = list(self.scopes[-1].values())
        out = []
        for relation in relations:
            if relation.columns is None:
                raise Opaque(item, f"the columns of {relation.alias}, an external reference, are not known")
            for name, t in relation.columns.items():
                out.append(Node("column", self.span(item), t, {"relation": relation.alias, "table": relation.table,
                                                               "column": name, "output_name": name}, role="output"))
        return out

    # ------------------------------------------------------------------ ORDER BY, OFFSET

    def finish(self, qe, plan: Node, outputs, key_for) -> Node:
        """ORDER BY and OFFSET ... FETCH of a query expression, after its rows are formed."""
        clause = qe.OrderByClause
        if clause is not None:
            keys = []
            for element in clause.OrderByElements:
                key = self.ruled(element.Expression, key_for(element))
                key.attrs = dict(key.attrs, direction="descending" if str(element.SortOrder) == "Descending"
                                 else "ascending")
                keys.append(as_role(key, "order"))
            plan = Node("sort", self.span(clause), None, {}, [as_role(plan, "input")] + keys)
        offset = getattr(qe, "OffsetClause", None)
        if offset is not None:
            children = [as_role(plan, "input"), as_role(self.expression(offset.OffsetExpression), "skip")]
            if offset.FetchExpression is not None:
                children.append(as_role(self.expression(offset.FetchExpression), "count"))
            plan = Node("skip_and_limit", self.span(offset), None, {}, children)
        return plan

    def output_key(self, expression, aliases: dict, outputs: list, outputs_only: bool) -> Node:
        """A sort key: an output column by its position or its name, else (in a query specification)
        an expression over the query's own rows."""
        kind = type_name(expression)
        if kind == "IntegerLiteral":
            position = int(str(expression.Value))
            if not 1 <= position <= len(outputs):
                raise Opaque(expression, f"ORDER BY position {position} is not a column of the select list")
            name, t = outputs[position - 1]
            return Node("column", self.span(expression), t, {"relation": OUTPUT, "table": None, "column": name})
        if kind == "ColumnReferenceExpression" and len(expression.MultiPartIdentifier.Identifiers) == 1:
            found = aliases.get(self.fold(str(expression.MultiPartIdentifier.Identifiers[0].Value)))
            if found:
                return Node("column", self.span(expression), found[1], {"relation": OUTPUT, "table": None,
                                                                         "column": found[0]})
        if outputs_only:
            raise Opaque(expression, "ORDER BY of a set operation must name a column of the result")
        return self.expression(expression)

    # ------------------------------------------------------------------ row sources

    def table_reference(self, ref) -> Node:
        kind = type_name(ref)
        method = getattr(self, "t_" + kind, None)
        if method is None:
            raise Opaque(ref, f"{kind} is not lowered yet")
        return method(ref)

    def register(self, ref, relation: Relation) -> None:
        key = self.fold(relation.alias)
        if key in self.scopes[-1]:
            raise Opaque(ref, f"alias {relation.alias} is used twice")
        self.scopes[-1][key] = relation

    def hints_of(self, hints) -> list[str]:
        return [self.text_of(h) for h in hints] if hints is not None and hints.Count else []

    def external(self, ref, name: str, reason: str, alias: str | None = None) -> Node:
        alias = alias or name.split(".")[-1]
        self.register(ref, Relation(alias, None, None))
        return Node("external_reference", self.span(ref), None, {"name": name, "reason": reason, "alias": alias})

    def t_NamedTableReference(self, ref) -> Node:
        obj = ref.SchemaObject
        parts: tuple[str, ...] = tuple(str(i.Value) for i in obj.Identifiers)
        alias = str(ref.Alias.Value) if ref.Alias is not None else (parts[-1] if parts else "")
        if obj.ServerIdentifier is not None:
            return self.external(ref, ".".join(parts), "a linked server", alias)
        if parts[-1].startswith("#"):
            return self.temp_table_reference(ref, parts, alias)
        if ref.TemporalClause is not None or ref.TableSampleClause is not None:
            raise Opaque(ref, "FOR SYSTEM_TIME and TABLESAMPLE are not lowered yet")
        if len(parts) == 1:
            for frame in reversed(self.ctes):
                relation = frame.get(self.fold(parts[0]))
                if relation is not None:
                    self.register(ref, Relation(alias, relation.columns, None))
                    attrs: dict = {"named_query": relation.alias, "alias": alias}
                    hints = self.hints_of(ref.TableHints)
                    return Node("read_table", self.span(ref), None, dict(attrs, **({"hints": hints} if hints else {})))
        if len(parts) == 1 and self.trigger_table and parts[0].casefold() in ("inserted", "deleted"):
            return self.changed_rows(ref, parts[0].casefold(), alias)
        context = dict(self.name_context, kinds={"table", "view"})
        try:
            table = self.reader.resolve(parts, context)
        except names.AmbiguousName as ambiguous:
            raise Opaque(ref, str(ambiguous)) from None
        if table is None:
            if parts[0].casefold() in ("sys", "information_schema"):
                return self.external(ref, ".".join(parts), "a system object", alias)
            if len(parts) >= 3 and not self.reader.snapshot.has(parts[0]):
                return self.external(ref, ".".join(parts), f"database {parts[0]}, which is not in the catalog", alias)
            raise Opaque(ref, f"{'.'.join(parts)} does not resolve to a table or view in the catalog")
        source_kind = self.reader.kind_of(table)
        shape = self.reader.value(table, "columns" if source_kind == "table" else "result_shape")
        self.register(ref, Relation(alias, {c["name"]: T.from_column(c) for c in shape}, table))
        attrs = {"table": table, "alias": alias, "source": source_kind}
        hints = self.hints_of(ref.TableHints)
        if hints:
            attrs["hints"] = hints
        return Node("read_table", self.span(ref), None, attrs)

    def temp_table_reference(self, ref, parts: tuple[str, ...], alias: str) -> Node:
        base = parts[-1]
        if base.startswith("##"):
            raise Opaque(ref, "a global temp table is not lowered yet: it is visible to every session")
        found = self.temp_tables.get(base.casefold()) if len(parts) == 1 else None
        if found is None:
            raise Opaque(ref, f"the temp table {'.'.join(parts)} is not in scope here: no statement before this one "
                              "in its batch creates it (a table made by a calling procedure is not visible to "
                              "lowering)")
        name, columns = found
        self.register(ref, Relation(alias, dict(columns), None))
        attrs: dict = {"temp_table": name, "alias": alias, "columns": list(columns), "source": "temp_table"}
        hints = self.hints_of(ref.TableHints)
        if hints:
            attrs["hints"] = hints
        return Node("read_table", self.span(ref), None, attrs)

    def t_VariableTableReference(self, ref) -> Node:
        name = str(ref.Variable.Name)
        found = self.row_sets.get(name.casefold())
        if found is None:
            raise Opaque(ref, f"{name} is not a table variable declared before this point in its batch")
        declared, columns = found
        alias = str(ref.Alias.Value) if ref.Alias is not None else declared
        self.register(ref, Relation(alias, dict(columns), None))
        return Node("read_table", self.span(ref), None, {"row_set": declared, "alias": alias,
                                                         "columns": list(columns), "source": "row_set"})

    def changed_rows(self, ref, which: str, alias: str) -> Node:
        """`inserted` and `deleted` in a trigger: the rows after, or before, the statement that fired it."""
        table = str(self.trigger_table)
        self.register(ref, Relation(alias, self.table_columns(table), table))
        return Node("read_changed_rows", self.span(ref), None, {"rows": which, "table": table, "alias": alias})

    def table_columns(self, identity: str) -> dict[str, VType]:
        kind = self.reader.kind_of(identity)
        shape = self.reader.value(identity, "columns" if kind == "table" else "result_shape")
        return {c["name"]: T.from_column(c) for c in shape}

    def t_JoinParenthesisTableReference(self, ref) -> Node:
        return self.table_reference(ref.Join)

    def t_QualifiedJoin(self, ref) -> Node:
        join_kind = JOINS.get(str(ref.QualifiedJoinType))
        if join_kind is None:
            raise Opaque(ref, f"{ref.QualifiedJoinType} join is not lowered yet")
        left = self.table_reference(ref.FirstTableReference)
        right = self.table_reference(ref.SecondTableReference)
        condition = self.predicate(ref.SearchCondition)
        attrs: dict = {"kind": join_kind}
        if str(ref.JoinHint) != "None":
            attrs["hints"] = [str(ref.JoinHint).upper()]
        return Node("join", self.span(ref), None, attrs,
                    [as_role(left, "left"), as_role(right, "right"), as_role(condition, "condition")])

    def t_UnqualifiedJoin(self, ref) -> Node:
        kind = str(ref.UnqualifiedJoinType)
        left = self.table_reference(ref.FirstTableReference)
        right = self.table_reference(ref.SecondTableReference)
        if kind == "CrossJoin":
            return Node("join", self.span(ref), None, {"kind": "cross"}, [as_role(left, "left"), as_role(right, "right")])
        if kind in ("CrossApply", "OuterApply"):
            return Node("lateral_join", self.span(ref), None, {"kind": "inner" if kind == "CrossApply" else "outer"},
                        [as_role(left, "left"), as_role(right, "right")])
        raise Opaque(ref, f"{kind} is not lowered yet")

    def t_QueryDerivedTable(self, ref) -> Node:
        if ref.Alias is None:
            raise Opaque(ref, "a derived table needs an alias")
        self.scopes.append({})
        try:
            plan, outputs = self.query_expression(ref.QueryExpression)
        finally:
            self.scopes.pop()
        outputs = self.renamed(ref, plan, outputs, ref.Columns)
        alias = str(ref.Alias.Value)
        self.register(ref, Relation(alias, dict(outputs), None))
        return Node("named_query", self.span(ref), None, {"name": alias}, [as_role(plan, "query")])

    def t_InlineDerivedTable(self, ref) -> Node:
        if ref.Alias is None or ref.Columns is None or not ref.Columns.Count:
            raise Opaque(ref, "a VALUES row source needs an alias and column names")
        rows = [[self.expression(v) for v in row.ColumnValues] for row in ref.RowValues]
        width = ref.Columns.Count
        if any(len(r) != width for r in rows):
            raise Opaque(ref, "the rows of VALUES have different numbers of values")
        converted = [self.meet(ref, *[row[j] for row in rows]) for j in range(width)]
        columns = [str(c.Value) for c in ref.Columns]
        types = [next((n.value_type for n in col if n.kind != "null"), T.INT) for col in converted]
        alias = str(ref.Alias.Value)
        self.register(ref, Relation(alias, dict(zip(columns, types)), None))
        cells = [as_role(converted[j][i], "value") for i in range(len(rows)) for j in range(width)]
        return Node("read_values", self.span(ref), None, {"columns": columns, "rows": len(rows), "alias": alias}, cells)

    def t_SchemaObjectFunctionTableReference(self, ref) -> Node:
        obj = ref.SchemaObject
        parts: tuple[str, ...] = tuple(str(i.Value) for i in obj.Identifiers)
        if len(parts) >= 2 and parts[-1].lower() == "nodes":
            return self.shred_xml(ref, list(parts[:-1]))
        context = dict(self.name_context, kinds={"inline_function", "table_function"})
        try:
            function = self.reader.resolve(parts, context)
        except names.AmbiguousName as ambiguous:
            raise Opaque(ref, str(ambiguous)) from None
        if function is None:
            raise Opaque(ref, f"{'.'.join(parts)} does not resolve to a table-valued function in the catalog")
        arguments = self.function_arguments(ref, function, list(ref.Parameters))
        shape = self.reader.value(function, "result_shape")
        outputs = [(c["name"], T.from_column(c)) for c in shape]
        outputs = self.renamed(ref, None, outputs, ref.Columns)
        alias = str(ref.Alias.Value) if ref.Alias is not None else str(obj.BaseIdentifier.Value)
        self.register(ref, Relation(alias, dict(outputs), None))
        return Node("call_table_function", self.span(ref), None,
                    {"function": function, "alias": alias, "columns": [name for name, _ in outputs]},
                    [as_role(a, "argument") for a in arguments])

    def shred_xml(self, ref, column: list[str]) -> Node:
        source = self.column(ref, column)
        if source.value_type.name != "xml":
            raise Opaque(ref, "nodes() on a column that is not xml is not lowered yet")
        params = list(ref.Parameters)
        path = self.expression(params[0]) if len(params) == 1 else None
        if path is None or path.kind != "literal" or "value" not in path.attrs or ref.Alias is None or ref.Columns is None or ref.Columns.Count != 1:
            raise Opaque(ref, "nodes() needs a string literal path, an alias and one column name")
        alias, name = str(ref.Alias.Value), str(ref.Columns[0].Value)
        self.register(ref, Relation(alias, {name: VType.of("xml")}, None))
        return Node("shred_xml", self.span(ref), None, {"path": path.attrs["value"], "alias": alias, "column": name,
                                                        "namespaces": list(self.xml_namespaces)},
                    [as_role(source, "argument")])

    def t_PivotedTableReference(self, ref) -> Node:
        if ref.Alias is None:
            raise Opaque(ref, "PIVOT needs an alias")
        self.scopes.append({})
        try:
            source = self.table_reference(ref.TableReference)
            relations = list(self.scopes[-1].values())
            if any(r.columns is None for r in relations):
                raise Opaque(ref, "PIVOT over an external reference is not lowered yet")
            function = self.text_of(ref.AggregateFunctionIdentifier).split(".")[-1].strip("[]").upper()
            operation = PIVOT_AGGREGATES.get(function)
            if operation is None or ref.ValueColumns.Count != 1:
                raise Opaque(ref, f"PIVOT with {function} is not lowered yet")
            value = self.expression(ref.ValueColumns[0])
            pivot = self.expression(ref.PivotColumn)
        finally:
            self.scopes.pop()
        result = {"sum": T.sum_type, "average": T.average_type, "minimum": lambda t: t, "maximum": lambda t: t,
                  "count_values": lambda t: T.INT}[operation](value.value_type)
        if result is None:
            raise Opaque(ref, f"PIVOT with {function} over {value.type} is not lowered yet")
        used = {(value.attrs["relation"], value.attrs["column"]), (pivot.attrs["relation"], pivot.attrs["column"])}
        columns: dict[str, VType] = {}
        for relation in relations:
            for name, t in (relation.columns or {}).items():
                if (relation.alias, name) in used:
                    continue
                if name in columns:
                    raise Opaque(ref, f"PIVOT over an input with two columns named {name} is not lowered yet")
                columns[name] = t
        values = [str(c.Value) for c in ref.InColumns]
        for v in values:
            columns[v] = result
        alias = str(ref.Alias.Value)
        self.register(ref, Relation(alias, columns, None))
        return Node("pivot", self.span(ref), None,
                    {"alias": alias, "aggregate": operation, "values": values, "columns": list(columns)},
                    [as_role(source, "input"), as_role(value, "argument"), as_role(pivot, "key")])

    def t_OpenQueryTableReference(self, ref) -> Node:
        return self.external(ref, str(ref.LinkedServer.Value), "a linked server (OPENQUERY)",
                             str(ref.Alias.Value) if ref.Alias is not None else None)
