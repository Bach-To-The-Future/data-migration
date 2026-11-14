"""Lowering of T-SQL procedural statements (spec sections 4 and 5, issue #43).

The statements of a procedure, trigger, batch or function body: variables (`declare_variable`, `assign`,
`assign_from_rows`), table variables (`declare_row_set_variable`), temp tables (`create_temp_table`,
`drop_temp_table`), control flow (`if`, `while`, `break`, `continue`, `jump`, `declare_label`, `return`), the
statements that read and write rows (`emit_result_set`, `insert_rows`, `update_rows`, `delete_rows`), calls
(`call_procedure`) and the two session options that matter here (`set_session_option`, `set_abort_on_error`).

Conventions of the nodes. A statement list is children with the role `statement`; an `if` has `condition`,
`then` and `else` children, a `while` has `condition` and `body`. A `declare_variable` has its type, and an
`initial` child where it gives a value; an `assign` names its `variable` and has the `argument` converted to
the variable's type; an `assign_from_rows` names its `variables` and has the `query` whose outputs they take. A
temp table or table variable is declared with `columns` (name, type, nullable, identity), `primary_key` and
`unique`; one is read as a `read_table` with `temp_table` or `row_set`. `insert_rows`, `update_rows` and
`delete_rows` name their `target`, its `target_kind` (`table`, `temp_table` or `row_set`) and `columns`; an
insert has a `source` (a plan, or `read_values`), an update or delete has the `input` rows it works on (the
row sources, joined, then filtered), its `target_alias` there, and an update has one `value` per column.

Scopes are not decided here: `core.scopes` derives them from the statements, and the checker derives them
again. What this module keeps is the lowering-time view of the same facts (which variables, table variables and
temp tables are declared) so that every name resolves to its declaration and its type.

A statement lowering cannot express is an opaque fragment. If it can change nothing but the names it mentions
(a PRINT, an INSERT whose OUTPUT clause is not lowered, a FETCH) it records those names in `touches` and is not a
scope barrier; any other (a transaction statement, a cursor declaration, a call to something unresolved) is a scope
barrier. TRY/CATCH, transactions, cursors, errors, messages, row counts and identity are lowered by `transactions`,
`cursors` and `errors`, which extend this class.
"""
from __future__ import annotations

import re

from ...core.ir import Node, VType
from . import collations, names
from . import types as T
from .expressions import Opaque, Relation, as_role, type_name
from .parser import walk
from .queries import Queries

PERMISSIONS = {"GrantStatement", "RevokeStatement", "DenyStatement", "RevertStatement"}
SCHEMA_CHANGES = {"CreateTableStatement", "AlterTableStatement", "DropTableStatement", "CreateIndexStatement",
                  "DropIndexStatement", "AlterIndexStatement", "CreateSchemaStatement", "DropSchemaStatement",
                  "CreateSequenceStatement", "AlterSequenceStatement", "DropSequenceStatement",
                  "CreateViewStatement", "DropViewStatement", "AlterViewStatement", "CreateProcedureStatement",
                  "DropProcedureStatement", "AlterProcedureStatement", "CreateSynonymStatement",
                  "DropSynonymStatement", "TruncateTableStatement", "AlterTableAddTableElementStatement",
                  "AlterTableDropTableElementStatement", "AlterTableSetStatement", "AlterTableAlterColumnStatement",
                  "AlterTableConstraintModificationStatement", "AlterTableSwitchStatement", "AlterTableRebuildStatement",
                  "DropTriggerStatement", "DropFunctionStatement", "CreateTriggerStatement", "CreateFunctionStatement",
                  "CreateTypeUddtStatement", "CreateTypeTableStatement", "DropTypeStatement",
                  "CreateStatisticsStatement", "DropStatisticsStatement", "UpdateStatisticsStatement"}
# Statements that can change nothing but the variables and tables they name: kept as written, they say what they
# touch and are not scope barriers. Every other statement kept as written is a barrier.
NAMED_ONLY = {"PrintStatement", "RaiseErrorStatement", "ThrowStatement", "SelectStatement", "InsertStatement",
              "UpdateStatement", "DeleteStatement", "MergeStatement", "SetVariableStatement", "ReturnStatement",
              "WaitForStatement", "CreateIndexStatement", "TruncateTableStatement", "FetchCursorStatement"}
COMPOUND = {"AddEquals": "Add", "SubtractEquals": "Subtract", "MultiplyEquals": "Multiply",
            "DivideEquals": "Divide", "ModEquals": "Modulo"}
BINARY_NAMES = {"Add": "add", "Subtract": "subtract", "Multiply": "multiply", "Divide": "divide", "Modulo": "modulo"}
OPTIONS = {"NoCount": "NOCOUNT"}
GLOBAL_TEMP = "a global temp table is not lowered yet: it is visible to every session"
TEMPDB_ASSUMPTION = ("The collation of tempdb is assumed to be the server's default collation: the catalog does not "
                     "read tempdb, and a temp table's text columns without COLLATE take tempdb's.")
NULLABLE_ASSUMPTION = ("A column of a temp table or table variable with neither NULL nor NOT NULL is assumed "
                       "nullable (ANSI_NULL_DFLT_ON, which every current client driver sets ON); a primary key "
                       "column is not.")


class Procedural(Queries):
    return_type: VType | None = None          # what a scalar function returns, set while lowering its body

    def begin_batch(self) -> None:
        """A batch of a script starts with no variables and no table variables; temp tables survive it."""
        self.variables, self.row_sets = {}, {}

    # ------------------------------------------------------------------ statement lists

    def statement_list(self, statements, role: str) -> list[Node]:
        out: list[Node] = []
        for s in statements:
            out += [as_role(n, role) for n in self.statement(s)]
        return out

    def statement(self, s) -> list[Node]:
        kind = type_name(s)
        method = getattr(self, "s_" + kind, None)
        try:
            if method is not None:
                return method(s)
            if kind in PERMISSIONS:
                return [Node("permission_change", self.span(s),
                             None, {"text": self.text_of(s), "manual": True, "scope_barrier": False})]
            if kind in SCHEMA_CHANGES:
                if self.temp_target(s):
                    raise Opaque(s, f"{kind} on a temp table is not lowered yet")
                return [self.schema_change(s)]
            raise Opaque(s, f"{kind} is not lowered yet: it is kept as written")
        except Opaque as opaque:
            return [self.opaque_statement(s, opaque.reason)]

    def schema_change(self, s) -> Node:
        """A statement that creates, alters or drops a permanent object: recorded, and manual work."""
        return Node("schema_change", self.span(s), None,
                    {"text": self.text_of(s), "manual": True, "scope_barrier": False})

    def temp_target(self, statement) -> bool:
        found = []

        def visit(f):
            if type_name(f) == "SchemaObjectName" and f.BaseIdentifier is not None \
                    and str(f.BaseIdentifier.Value).startswith("#"):
                found.append(f)
        walk(statement, visit)
        return bool(found)

    def opaque_statement(self, s, reason: str) -> Node:
        """A statement kept as written: a scope barrier unless it can change nothing but what it names."""
        barrier = type_name(s) not in NAMED_ONLY
        node = self.opaque(s, reason, barrier=barrier)
        node.attrs["touches"] = self.touches(s)
        return node

    def touches(self, s) -> dict:
        variables: dict[str, str] = {}
        temp_tables: dict[str, str] = {}

        def visit(f):
            kind = type_name(f)
            if kind == "VariableReference":
                name = str(f.Name)
                variables.setdefault(name.casefold(), name)
            elif kind == "SchemaObjectName" and f.BaseIdentifier is not None and f.SchemaIdentifier is None \
                    and str(f.BaseIdentifier.Value).startswith("#"):
                name = str(f.BaseIdentifier.Value)
                temp_tables.setdefault(name.casefold(), name)
        walk(s, visit)
        spelled_variables = [self.variables.get(k, self.row_sets.get(k, (v,)))[0] for k, v in variables.items()]
        spelled_tables = [self.temp_tables[k][0] if k in self.temp_tables else v for k, v in temp_tables.items()]
        declared = {k for k in variables if k in self.variables or k in self.row_sets}
        undeclared = [v for k, v in variables.items() if k not in declared]
        undeclared += [v for k, v in temp_tables.items() if k not in self.temp_tables]
        return {"variables": sorted(spelled_variables), "temp_tables": sorted(spelled_tables),
                "undeclared": sorted(undeclared)}

    # ------------------------------------------------------------------ blocks and control flow

    def s_BeginEndBlockStatement(self, s) -> list[Node]:
        out: list[Node] = []
        for inner in s.StatementList.Statements:
            out += self.statement(inner)
        return out

    def condition(self, predicate) -> Node:
        try:
            return as_role(self.predicate(predicate), "condition")
        except Opaque as opaque:
            return self.opaque(predicate, opaque.reason, "condition")

    def s_IfStatement(self, s) -> list[Node]:
        condition = self.condition(s.Predicate)
        before = dict(self.temp_tables)
        then = self.statement_list([s.ThenStatement], "then")
        after_then, self.temp_tables = self.temp_tables, dict(before)
        other = self.statement_list([s.ElseStatement], "else") if s.ElseStatement is not None else []
        self.temp_tables = self.merge_temp_tables(before, after_then, self.temp_tables)
        return [Node("if", self.span(s), None, {}, [condition] + then + other)]

    def s_WhileStatement(self, s) -> list[Node]:
        condition = self.condition(s.Predicate)
        before = dict(self.temp_tables)
        body = self.statement_list([s.Statement], "body")
        self.temp_tables = self.merge_temp_tables(before, self.temp_tables, before)       # it may run no times
        return [Node("while", self.span(s), None, {}, [condition] + body)]

    def merge_temp_tables(self, before: dict, *paths: dict) -> dict:
        """The temp tables that may exist after alternative paths (the same rule as core.scopes)."""
        merged = {k: next(p[k] for p in paths if k in p) for k in before if any(k in p for p in paths)}
        for p in paths:
            for k, v in p.items():
                merged.setdefault(k, v)
        return merged

    def s_BreakStatement(self, s) -> list[Node]:
        return [Node("break", self.span(s))]

    def s_ContinueStatement(self, s) -> list[Node]:
        return [Node("continue", self.span(s))]

    def s_LabelStatement(self, s) -> list[Node]:
        return [Node("declare_label", self.span(s), None, {"name": str(s.Value).rstrip(":")})]

    def s_GoToStatement(self, s) -> list[Node]:
        return [Node("jump", self.span(s), None, {"label": str(s.LabelName.Value)})]

    def s_ReturnStatement(self, s) -> list[Node]:
        children = []
        if s.Expression is not None:
            value = self.lowered_or_opaque(s.Expression)
            if self.return_type is not None and value.kind != "opaque_fragment":
                value = self.store(value, self.return_type)      # a function returns its declared type
            children.append(as_role(value, "argument"))
        return [Node("return", self.span(s), None, {}, children)]

    def lowered_or_opaque(self, fragment) -> Node:
        try:
            return self.expression(fragment)
        except Opaque as opaque:
            return self.opaque(fragment, opaque.reason)

    def store(self, node: Node, target: VType) -> Node:
        """The value as it is once stored in something of type `target` (a variable, a column, a returned value):
        converted to the type, and cut, padded or rounded to fit it where SQL Server does that without an error
        (a string too long for its variable, a decimal with fewer digits)."""
        node = self.convert_toward(node, target)
        value = node.type
        if node.kind in ("null", "opaque_fragment") or value is None or value == target:
            return node
        narrower = False
        if value.name == "string" and target.name == "string":
            wanted, held = target.length(), value.length()
            narrower = wanted != held and (wanted != T.UNLIMITED) and (
                held == T.UNLIMITED or target.flag("fixed") or int(wanted) < int(held))
        elif value.name == "decimal" and target.name == "decimal":
            narrower = (target.int_param("precision"), target.int_param("scale")) != (
                value.int_param("precision"), value.int_param("scale"))
        if not narrower:
            return node
        return Node("implicit_convert", node.span, target, {"fits": True}, [as_role(node, "argument")])

    # ------------------------------------------------------------------ variables

    def s_DeclareVariableStatement(self, s) -> list[Node]:
        out = []
        for d in s.Declarations:
            name = str(d.VariableName.Value)
            t = self.data_type(d.DataType)
            children = []
            if d.Value is not None:
                value = self.lowered_or_opaque(d.Value)
                if value.kind != "opaque_fragment":
                    value = self.store(value, t)
                children.append(as_role(value, "initial"))
            self.variables[name.casefold()] = (name, t)
            out.append(Node("declare_variable", self.span(d), t, {"name": name}, children))
        return out

    def s_SetVariableStatement(self, s) -> list[Node]:
        if s.Variable is None or s.CursorDefinition is not None:
            raise Opaque(s, "SET of a cursor or a property is not lowered yet")
        name = str(s.Variable.Name)
        found = self.variables.get(name.casefold())
        if found is None:
            raise Opaque(s, f"{name} is not a parameter or a variable declared before this point in its batch: it "
                            "may have been declared by a statement kept as written, or in another batch")
        declared, t = found
        kind = str(s.AssignmentKind)
        try:
            value = self.expression(s.Expression)
            if kind != "Equals":
                op = COMPOUND.get(kind)
                if op is None:
                    raise Opaque(s, f"the assignment {kind} is not lowered yet")
                current = self.node("variable", s.Variable, t, {"name": declared})
                value = self.arithmetic(s, BINARY_NAMES[op], current, value)
            value = self.store(value, t)
        except Opaque as opaque:
            value = self.opaque(s.Expression, opaque.reason)
        return [Node("assign", self.span(s), None, {"variable": declared}, [as_role(value, "argument")])]

    def s_DeclareTableVariableStatement(self, s) -> list[Node]:
        name = str(s.Body.VariableName.Value)
        label = collations.Label(self.reader.value(self.database, "collation"), collations.COLUMN)
        columns, types, keys = self.table_definition(s.Body.Definition, label)
        self.row_sets[name.casefold()] = (name, types)
        return [Node("declare_row_set_variable", self.span(s), None, dict({"name": name, "columns": columns}, **keys))]

    def table_definition(self, definition, label: collations.Label, *, tempdb: bool = False) \
            -> tuple[list[dict], dict[str, VType], dict]:
        """The columns of a temp table or table variable: name, type, nullability and identity, and its keys.
        What this does not read (defaults, checks, computed columns, foreign keys, indexes) is opaque."""
        columns: list[dict] = []
        types: dict[str, VType] = {}
        primary: list[str] = []
        unique: list[list[str]] = []
        nullable_assumed = False
        for cd in definition.ColumnDefinitions:
            name = str(cd.ColumnIdentifier.Value)
            if cd.DefaultConstraint is not None or cd.ComputedColumnExpression is not None or cd.GeneratedAlways is not None:
                raise Opaque(cd, f"column {name}: a default, a computed column or a generated column is not lowered yet")
            column_label = collations.Label(str(cd.Collation.Value), collations.COLUMN) if cd.Collation is not None else label
            t = self.data_type(cd.DataType, label=column_label)
            nullable, in_key = None, False
            for c in cd.Constraints:
                kind = type_name(c)
                if kind == "NullableConstraintDefinition":
                    nullable = bool(c.Nullable)
                elif kind == "UniqueConstraintDefinition":
                    in_key = True
                    if c.IsPrimaryKey:
                        primary.append(name)
                    else:
                        unique.append([name])
                else:
                    raise Opaque(c, f"column {name}: {kind} is not lowered yet")
            if nullable is None:
                nullable = not (in_key and name in primary)
                nullable_assumed = nullable_assumed or nullable
            elif in_key and name in primary and nullable:
                raise Opaque(cd, f"column {name}: a primary key column cannot be nullable")
            identity = None
            if cd.IdentityOptions is not None:
                options = cd.IdentityOptions
                identity = {"seed": int(str(options.IdentitySeed.Value)) if options.IdentitySeed is not None else 1,
                            "increment": int(str(options.IdentityIncrement.Value))
                            if options.IdentityIncrement is not None else 1}
            if tempdb and T.is_string(t) and cd.Collation is None:
                self.assumptions.add(TEMPDB_ASSUMPTION)
            columns.append({"name": name, "type": t.to_json(), "nullable": nullable and name not in primary,
                            "identity": identity})
            types[name] = t
        for constraint in definition.TableConstraints:
            if type_name(constraint) != "UniqueConstraintDefinition":
                raise Opaque(constraint, f"{type_name(constraint)} is not lowered yet")
            members = [str(list(c.Column.MultiPartIdentifier.Identifiers)[-1].Value) for c in constraint.Columns]
            if constraint.IsPrimaryKey:
                primary += members
            else:
                unique.append(members)
        if getattr(definition, "Indexes", None) is not None and definition.Indexes.Count:
            raise Opaque(definition, "an index declared inside the table definition is not lowered yet")
        for column in columns:
            if column["name"] in primary:
                column["nullable"] = False
        if nullable_assumed:
            self.assumptions.add(NULLABLE_ASSUMPTION)
        keys: dict = {}
        if primary:
            keys["primary_key"] = primary
        if unique:
            keys["unique"] = unique
        return columns, types, keys

    # ------------------------------------------------------------------ temp tables

    def temp_name(self, obj) -> str | None:
        if obj.BaseIdentifier is None or obj.SchemaIdentifier is not None:
            return None
        name = str(obj.BaseIdentifier.Value)
        return name if name.startswith("#") else None

    def s_CreateTableStatement(self, s) -> list[Node]:
        name = self.temp_name(s.SchemaObjectName)
        if name is None:
            return [self.schema_change(s)]
        if name.startswith("##"):
            raise Opaque(s, GLOBAL_TEMP)
        if s.AsFileTable or s.AsEdge or s.AsNode or s.Definition is None:
            raise Opaque(s, "this kind of CREATE TABLE is not lowered yet")
        label = collations.Label(self.server_collation(), collations.COLUMN)
        columns, types, keys = self.table_definition(s.Definition, label, tempdb=True)
        self.temp_tables[name.casefold()] = (name, types)
        return [Node("create_temp_table", self.span(s), None, dict({"name": name, "columns": columns}, **keys))]

    def server_collation(self) -> str:
        servers = [o for o in self.reader.snapshot.objects if o.kind == "server" and o.has_fact("collation")]
        if not servers:
            raise Opaque(self.parsed.tree, "the server's default collation, which tempdb's text columns take, is not "
                                           "in the catalog")
        return str(servers[0].fact("collation").value)

    def s_DropTableStatement(self, s) -> list[Node]:
        objects = list(s.Objects)
        temp = [self.temp_name(o) for o in objects]
        if any(temp) and not all(temp):
            raise Opaque(s, "DROP TABLE of temp and permanent tables in one statement is not lowered yet")
        if not all(temp):
            return [self.schema_change(s)]
        out = []
        for name in temp:
            if str(name).startswith("##"):
                raise Opaque(s, GLOBAL_TEMP)
            found = self.temp_tables.pop(str(name).casefold(), None)
            out.append(Node("drop_temp_table", self.span(s), None,
                            {"name": found[0] if found else name, "if_exists": bool(s.IsIfExists)}))
        return out

    # ------------------------------------------------------------------ SELECT

    def s_SelectStatement(self, s) -> list[Node]:
        if s.Into is not None:
            return [self.select_into(s)]
        spec = s.QueryExpression
        elements = list(spec.SelectElements) if type_name(spec) == "QuerySpecification" else []
        assigning = [e for e in elements if type_name(e) == "SelectSetVariable"]
        if assigning:
            if len(assigning) != len(elements):
                raise Opaque(s, "a SELECT that assigns variables cannot also return columns")
            plan, _ = self.select_statement(s)
            variables = [self.variables[str(e.Variable.Name).casefold()][0] for e in assigning]
            return [Node("assign_from_rows", self.span(s), None, {"variables": variables}, [as_role(plan, "query")])]
        plan, _ = self.select_statement(s)
        return [Node("emit_result_set", self.span(s), None, {}, [as_role(plan, "query")])]

    def select_into(self, s) -> Node:
        target = s.Into
        name = self.temp_name(target)
        if name is None:
            raise Opaque(s, "SELECT INTO a permanent table creates a table: it is not lowered")
        if name.startswith("##"):
            raise Opaque(s, GLOBAL_TEMP)
        plan, outputs = self.select_statement(s, into=True)
        types = dict(outputs)
        if len(types) != len(outputs):
            raise Opaque(s, "SELECT INTO needs a distinct name for every column")
        columns = [{"name": n, "type": t.to_json(), "nullable": None, "identity": None} for n, t in outputs]
        self.temp_tables[name.casefold()] = (name, types)
        return Node("create_temp_table", self.span(s), None, {"name": name, "columns": columns, "from_query": True},
                    [as_role(plan, "query")])

    # ------------------------------------------------------------------ INSERT, UPDATE, DELETE

    def write_target(self, ref) -> tuple[str, str, dict[str, VType], list[str]]:
        """The kind, name, columns and insertable columns (in order) of what a statement writes."""
        kind = type_name(ref)
        if kind == "VariableTableReference":
            name = str(ref.Variable.Name)
            found = self.row_sets.get(name.casefold())
            if found is None:
                raise Opaque(ref, f"{name} is not a table variable declared before this point in its batch")
            columns = dict(found[1])
            return "row_set", found[0], columns, list(columns)
        if kind != "NamedTableReference":
            raise Opaque(ref, f"{kind} as the target of a write is not lowered yet")
        obj = ref.SchemaObject
        parts = tuple(str(i.Value) for i in obj.Identifiers)
        if obj.ServerIdentifier is not None:
            raise Opaque(ref, "writing through a linked server is not lowered yet")
        if parts[-1].startswith("#"):
            if parts[-1].startswith("##"):
                raise Opaque(ref, GLOBAL_TEMP)
            found = self.temp_tables.get(parts[-1].casefold()) if len(parts) == 1 else None
            if found is None:
                raise Opaque(ref, f"the temp table {'.'.join(parts)} is not in scope here: no statement before this "
                                  "one in its batch creates it")
            return "temp_table", found[0], dict(found[1]), [n for n in found[1]]
        try:
            table = self.reader.resolve(parts, dict(self.name_context, kinds={"table"}))
        except names.AmbiguousName as ambiguous:
            raise Opaque(ref, str(ambiguous)) from None
        if table is None:
            raise Opaque(ref, f"{'.'.join(parts)} does not resolve to a table in the catalog")
        shape = self.reader.value(table, "columns")
        columns = {c["name"]: T.from_column(c) for c in shape}
        insertable = [c["name"] for c in shape if not c.get("identity") and not c.get("computed")
                      and c.get("system_type") not in ("timestamp", "rowversion")]
        return "table", table, columns, insertable

    def find(self, columns: dict[str, VType], name: str, where) -> str:
        found = [c for c in columns if self.fold(c) == self.fold(name)]
        if len(found) != 1:
            raise Opaque(where, f"column {name} is not a column of the target")
        return found[0]

    def s_InsertStatement(self, s) -> list[Node]:
        spec = s.InsertSpecification
        if spec.OutputClause is not None or spec.OutputIntoClause is not None:
            raise Opaque(s, "the OUTPUT clause of INSERT is not lowered yet")
        if spec.TopRowFilter is not None:
            raise Opaque(s, "INSERT TOP is not lowered yet")
        self.ctes.append({})
        try:
            definitions = self.common_tables(s)
            return self.insert_rows(s, spec, definitions)
        finally:
            self.ctes.pop()

    def common_tables(self, s) -> list[Node]:
        """The common table expressions written before INSERT, UPDATE or DELETE, as definitions of the write."""
        with_ = s.WithCtesAndXmlNamespaces
        if with_ is None:
            return []
        if with_.XmlNamespaces is not None:
            raise Opaque(s, "XML namespaces before a write are not lowered yet")
        return [self.common_table_expression(c) for c in with_.CommonTableExpressions]

    def insert_rows(self, s, spec, definitions: list[Node]) -> list[Node]:
        kind, target, columns, insertable = self.write_target(spec.Target)
        listed = [self.find(columns, str(list(c.MultiPartIdentifier.Identifiers)[-1].Value), s) for c in spec.Columns] \
            if spec.Columns is not None and spec.Columns.Count else insertable
        source = spec.InsertSource
        source_kind = type_name(source)
        wanted = [columns[c] for c in listed]
        if source_kind == "ValuesInsertSource":
            if source.IsDefaultValues:
                raise Opaque(s, "INSERT ... DEFAULT VALUES is not lowered yet")
            rows = [[self.expression(v) for v in row.ColumnValues] for row in source.RowValues]
            if any(len(r) != len(listed) for r in rows):
                raise Opaque(s, "a row of VALUES has a different number of values than the columns")
            cells = [as_role(self.store(rows[i][j], wanted[j]), "value")
                     for i in range(len(rows)) for j in range(len(listed))]
            plan = Node("read_values", self.span(source), None, {"columns": listed, "rows": len(rows), "alias": None},
                        cells)
        elif source_kind == "SelectInsertSource":
            plan, outputs = self.query_expression(source.Select)
            if len(outputs) != len(listed):
                raise Opaque(s, "the query returns a different number of columns than the INSERT names")
            self.convert_outputs(plan, wanted)
        else:
            raise Opaque(s, f"{source_kind} as the source of INSERT is not lowered yet")
        return [Node("insert_rows", self.span(s), None, {"target": target, "target_kind": kind, "columns": listed},
                     [as_role(plan, "source")] + definitions)]

    def convert_outputs(self, plan: Node, wanted: list[VType]) -> None:
        """Write out the conversions an INSERT makes of a query's columns to the target's types."""
        if plan.kind in ("project", "aggregate"):
            for i, output in enumerate(plan.children_with("output")):
                if output.kind == "opaque_fragment":
                    continue
                converted = self.store(output, wanted[i])
                if converted is not output:
                    converted.role = "output"
                    converted.attrs = dict(converted.attrs, output_name=output.attrs["output_name"])
                    plan.children[plan.children.index(output)] = converted
        elif plan.kind in ("union", "intersect", "except"):
            self.convert_outputs(plan.child("left"), wanted)
            self.convert_outputs(plan.child("right"), wanted)
        elif plan.children_with("input"):
            self.convert_outputs(plan.child("input"), wanted)

    def dml_input(self, spec, s, *, assigning: bool = False) -> tuple[Node, str, str, str, list[tuple[str, Node]]]:
        """The rows an UPDATE or DELETE works on (its row sources joined and filtered), the alias, kind and name of
        its target in them, and for an UPDATE (`assigning`) the column and value of each SET."""
        if spec.OutputClause is not None or spec.OutputIntoClause is not None:
            raise Opaque(s, "the OUTPUT clause is not lowered yet")
        if spec.TopRowFilter is not None:
            raise Opaque(s, "TOP in a write is not lowered yet")
        self.scopes.append({})
        try:
            if spec.FromClause is not None:
                refs = [self.table_reference(r) for r in spec.FromClause.TableReferences]
                source = refs[0]
                for extra in refs[1:]:
                    source = Node("join", self.span(spec.FromClause), None, {"kind": "cross"},
                                  [as_role(source, "left"), as_role(extra, "right")])
                alias, node = self.target_in(spec.Target, source)
            else:
                source = self.table_reference(spec.Target)
                alias, node = source.attrs["alias"], source
            if node.attrs.get("source") == "view":
                raise Opaque(s, "writing through a view is not lowered yet")
            if node.attrs.get("temp_table"):
                kind, target = "temp_table", node.attrs["temp_table"]
            elif node.attrs.get("row_set"):
                kind, target = "row_set", node.attrs["row_set"]
            elif node.attrs.get("table"):
                kind, target = "table", node.attrs["table"]
            else:
                raise Opaque(s, "the target of the write is not a table")
            relation = self.scopes[-1][self.fold(alias)]
            if spec.WhereClause is not None:
                if spec.WhereClause.Cursor is not None:
                    raise Opaque(s, "WHERE CURRENT OF is not lowered yet")
                condition = self.predicate(spec.WhereClause.SearchCondition)
                source = Node("filter", self.span(spec.WhereClause), None, {},
                              [as_role(source, "input"), as_role(condition, "condition")])
            values = self.set_values(spec, relation) if assigning else []
        finally:
            self.scopes.pop()
        return source, alias, kind, target, values

    def target_in(self, target, source: Node) -> tuple[str, Node]:
        """Which row source of the FROM clause an UPDATE or DELETE target names (an alias, or the table's name)."""
        if type_name(target) == "VariableTableReference":
            wanted = str(target.Variable.Name).casefold()
            matches = [n for n in source.walk() if n.kind == "read_table" and n.attrs.get("row_set", "").casefold() == wanted]
        else:
            parts = tuple(str(i.Value) for i in target.SchemaObject.Identifiers)
            matches = [n for n in source.walk() if n.kind == "read_table" and len(parts) == 1
                       and self.fold(n.attrs.get("alias", "")) == self.fold(parts[0])]
            if not matches:
                resolved = None
                if not parts[-1].startswith("#"):
                    try:
                        resolved = self.reader.resolve(parts, dict(self.name_context, kinds={"table", "view"}))
                    except names.AmbiguousName:
                        resolved = None
                matches = [n for n in source.walk() if n.kind == "read_table" and (
                    (resolved and n.attrs.get("table") == resolved) or
                    (parts[-1].startswith("#") and n.attrs.get("temp_table", "").casefold() == parts[-1].casefold()))]
        if len(matches) != 1:
            raise Opaque(target, "the target of the write is not exactly one of the row sources in FROM")
        return matches[0].attrs["alias"], matches[0]

    def set_values(self, spec, relation: Relation) -> list[tuple[str, Node]]:
        out = []
        columns = relation.columns or {}
        for clause in spec.SetClauses:
            if type_name(clause) != "AssignmentSetClause" or clause.Variable is not None:
                raise Opaque(clause, f"{type_name(clause)} in SET is not lowered yet")
            if str(clause.AssignmentKind) != "Equals":
                raise Opaque(clause, "a compound assignment in UPDATE is not lowered yet")
            column = self.find(columns, str(list(clause.Column.MultiPartIdentifier.Identifiers)[-1].Value), clause)
            value = self.lowered_or_opaque(clause.NewValue)
            if value.kind != "opaque_fragment":
                value = self.store(value, columns[column])
            out.append((column, as_role(value, "value")))
        return out

    def s_UpdateStatement(self, s) -> list[Node]:
        spec = s.UpdateSpecification
        self.ctes.append({})
        try:
            definitions = self.common_tables(s)
            source, alias, kind, target, values = self.dml_input(spec, s, assigning=True)
        finally:
            self.ctes.pop()
        return [Node("update_rows", self.span(s), None,
                     {"target": target, "target_kind": kind, "target_alias": alias, "columns": [c for c, _ in values]},
                     [as_role(source, "input")] + [v for _, v in values] + definitions)]

    def s_DeleteStatement(self, s) -> list[Node]:
        self.ctes.append({})
        try:
            definitions = self.common_tables(s)
            source, alias, kind, target, _ = self.dml_input(s.DeleteSpecification, s)
        finally:
            self.ctes.pop()
        return [Node("delete_rows", self.span(s), None,
                     {"target": target, "target_kind": kind, "target_alias": alias},
                     [as_role(source, "input")] + definitions)]

    # ------------------------------------------------------------------ calls and session options

    def s_ExecuteStatement(self, s) -> list[Node]:
        dynamic = self.dynamic_sql(s)
        if dynamic is not None:
            return [dynamic]
        spec = s.ExecuteSpecification
        entity = spec.ExecutableEntity
        if type_name(entity) != "ExecutableProcedureReference" or entity.ProcedureReference is None \
                or entity.ProcedureReference.ProcedureReference is None:
            raise Opaque(s, "EXEC of a procedure named by a variable is not lowered yet")
        if spec.ExecuteContext is not None or entity.ProcedureReference.ProcedureVariable is not None:
            raise Opaque(s, "EXEC AS or a procedure number is not lowered yet")
        obj = entity.ProcedureReference.ProcedureReference.Name
        parts = tuple(str(i.Value) for i in obj.Identifiers)
        if obj.ServerIdentifier is not None:
            raise Opaque(s, "EXEC through a linked server is not lowered yet")
        try:
            procedure = self.reader.resolve(parts, dict(self.name_context, kinds={"procedure"}))
        except names.AmbiguousName as ambiguous:
            raise Opaque(s, str(ambiguous)) from None
        if procedure is None:
            raise Opaque(s, f"{'.'.join(parts)} does not resolve to a procedure in the catalog")
        declared = self.reader.value(procedure, "parameters") if self.reader.has_fact(procedure, "parameters") else None
        if declared is None:
            raise Opaque(s, f"the parameters of {procedure} are not in the catalog")
        return_variable = None
        if spec.Variable is not None:
            found = self.variables.get(str(spec.Variable.Name).casefold())
            if found is None:
                raise Opaque(s, f"{spec.Variable.Name} is not declared before this point in its batch")
            return_variable = found[0]
        arguments = []
        for position, p in enumerate(entity.Parameters):
            named = str(p.Variable.Name) if p.Variable is not None else None
            fact = next((d for d in declared if named and d["name"].casefold() == named.casefold()), None) \
                if named else (declared[position] if position < len(declared) else None)
            if fact is None:
                raise Opaque(p, "an argument does not match a parameter of the procedure")
            value = self.lowered_or_opaque(p.ParameterValue)
            if p.IsOutput:
                if value.kind != "variable":
                    raise Opaque(p, "an OUTPUT argument must be a variable")
            elif value.kind != "opaque_fragment" and fact["system_type"] != "table":
                value = self.convert_toward(value, T.from_column(fact, default=self.default_label))
            node = as_role(value, "argument")
            node.attrs = dict(node.attrs, parameter=fact["name"], output=bool(p.IsOutput), named=named is not None)
            arguments.append(node)
        return [Node("call_procedure", self.span(s), None, {"procedure": procedure, "return_variable": return_variable,
                                                             "may_touch_temp_tables": True}, arguments)]

    def dynamic_sql(self, statement) -> Node | None:
        """EXEC (string) and sp_executesql: the expressions that build the text are lowered; the statement is a
        scope barrier and always manual work."""
        spec = statement.ExecuteSpecification
        entity = spec.ExecutableEntity
        children, form = [], None
        if type_name(entity) == "ExecutableStringList":
            form = "execute string"
            children = [as_role(self.joined_text(entity), "text")]
        elif type_name(entity) == "ExecutableProcedureReference" and entity.ProcedureReference is not None \
                and entity.ProcedureReference.ProcedureReference is not None \
                and str(entity.ProcedureReference.ProcedureReference.Name.BaseIdentifier.Value).lower() == "sp_executesql":
            form = "sp_executesql"
            params = [p.ParameterValue for p in entity.Parameters]
            if not params:
                return None
            children = [as_role(self.lowered_or_opaque(params[0]), "text")]
            children += [as_role(self.lowered_or_opaque(p), "parameter") for p in params[1:]]
        if form is None:
            return None
        return Node("dynamic_sql_execution", self.span(statement), None,
                    {"form": form, "scope_barrier": True, "manual": True}, children)

    def joined_text(self, entity) -> Node:
        """EXEC (a + b + ...) runs the pieces joined as strings are joined with +."""
        try:
            pieces = [self.expression(x) for x in entity.Strings]
            text = pieces[0]
            for piece in pieces[1:]:
                text = self.concatenate(entity, text, piece)
            return text
        except Opaque as opaque:
            return self.opaque(entity, opaque.reason)

    def s_PredicateSetStatement(self, s) -> list[Node]:
        options = str(s.Options).split(",")
        out = []
        for option in [o.strip() for o in options]:
            if option == "XactAbort":
                out.append(Node("set_abort_on_error", self.span(s), None, {"value": bool(s.IsOn)}))
            elif option in OPTIONS:
                out.append(Node("set_session_option", self.span(s), None,
                                {"option": OPTIONS[option], "value": bool(s.IsOn)}))
            else:
                raise Opaque(s, f"SET {re.sub(r'(?<!^)(?=[A-Z])', '_', option).upper()} changes how later statements "
                                "behave: it is not lowered yet")
        return out
