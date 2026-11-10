"""Lowering for SQL Server views and functions (spec sections 4 and 5).

Deterministic and faithful: every node gets the vocabulary operation that means exactly what the
T-SQL means, typed from the catalog, with its source position (expressions.py and queries.py hold
the rules). What lowering cannot express is kept verbatim as an opaque fragment with its reason,
never dropped: the whole query when its shape is not handled, otherwise the one select item.

Artefacts handled here: a view (`view_body`, one query), an inline table-valued function
(`table_function_body`, one query), and the outer record, parameters and `RETURN` of a scalar or
multi-statement function. The procedural statements of a function body (DECLARE, SET, IF, WHILE,
INSERT into a table variable) are lowered by ticket 08a (#43): until then each is an opaque fragment
that is a scope barrier, and dynamic SQL has its own node kind.
"""
from __future__ import annotations

from collections.abc import Callable

from ...core import vocabulary
from ...core.catalog import CatalogReader
from ...core.ir import Artefact, Node
from . import names
from . import types as T
from .expressions import Opaque, as_role, type_name
from .parser import Parsed, walk
from .queries import Queries

VIEWS = {"CreateViewStatement", "CreateOrAlterViewStatement", "AlterViewStatement"}
FUNCTIONS = {"CreateFunctionStatement", "CreateOrAlterFunctionStatement", "AlterFunctionStatement"}
PERMISSIONS = {"GrantStatement", "RevokeStatement", "DenyStatement", "RevertStatement"}
SCHEMA_CHANGES = {"CreateTableStatement", "AlterTableStatement", "DropTableStatement", "CreateIndexStatement",
                  "DropIndexStatement", "AlterIndexStatement", "CreateSchemaStatement", "DropSchemaStatement",
                  "CreateSequenceStatement", "AlterSequenceStatement", "DropSequenceStatement",
                  "CreateViewStatement", "DropViewStatement", "AlterViewStatement", "CreateProcedureStatement",
                  "DropProcedureStatement", "AlterProcedureStatement", "CreateSynonymStatement",
                  "DropSynonymStatement", "TruncateTableStatement"}
PROCEDURAL = ("the statement is lowered by ticket 08a (#43), procedural lowering: until then it is kept as written, "
              "and everything it might have touched is unknown from here on")


class Lowering(Queries):
    def __init__(self, reader: CatalogReader, identity: str, parsed: Parsed):
        super().__init__(reader, identity, parsed)
        self.opaque_dependencies: set[str] = set()

    # ------------------------------------------------------------------ artefact

    def lower(self) -> Artefact:
        [batch] = list(self.parsed.tree.Batches)
        [statement] = list(batch.Statements)
        kind = type_name(statement)
        artefact_type = self.reader.kind_of(self.identity)
        if kind in VIEWS and artefact_type == "view":
            body = self.view(statement)
        elif kind in FUNCTIONS and artefact_type in ("scalar_function", "inline_function", "table_function"):
            body = self.function(statement, artefact_type)
        else:
            raise ValueError(f"{self.identity} is a {artefact_type}, but its text is a {kind}")
        artefact = Artefact(self.identity, artefact_type, self.parsed.text, body, dependencies=self.dependencies(body),
                            vocabulary_version=vocabulary.version())
        for n in body.walk():
            if n.kind == "null" and n.type is not None and n.type.name == "unknown":
                n.type = T.INT                  # a NULL nothing types is an int, as SQL Server types it
        self.attach_comments(body)
        artefact.secret_settings = sorted(self.settings)
        artefact.assumptions = sorted(self.assumptions)
        artefact.number_nodes()
        artefact.hints = self.hints(body)
        return artefact

    def stored_settings(self, statement) -> dict:
        options = [self.text_of(o) for o in (getattr(statement, "ViewOptions", None) or getattr(statement, "Options", None) or [])]
        if getattr(statement, "WithCheckOption", False):
            options.append("WITH CHECK OPTION")
        return {"options": options,
                "ansi_nulls": bool(self.reader.value(self.identity, "ansi_nulls", "sqlserver")),
                "quoted_identifier": bool(self.reader.value(self.identity, "quoted_identifier", "sqlserver"))}

    def view(self, statement) -> Node:
        select = statement.SelectStatement
        try:
            if not self.reader.value(self.identity, "ansi_nulls", "sqlserver"):
                raise Opaque(select, "a view created with ANSI_NULLS OFF is not lowered yet")
            plan, outputs = self.select_statement(select)
            columns = statement.Columns
            if columns is not None and columns.Count:
                if columns.Count != len(outputs):
                    raise Opaque(select, "the view's column list names a different number of columns than the query "
                                         "returns")
                self.rename_outputs(plan, [str(c.Value) for c in columns])
        except Opaque as opaque:
            plan = self.opaque(select, opaque.reason)
        plan.role = "query"
        return Node("view_body", self.span(statement), None, dict(self.stored_settings(statement), name=self.identity),
                    [plan])

    # ------------------------------------------------------------------ functions

    def function(self, statement, artefact_type: str) -> Node:
        children = self.parameters(statement)
        return_type = statement.ReturnType
        attrs: dict = dict(self.stored_settings(statement), name=self.identity)
        if artefact_type == "scalar_function":
            body = "scalar_function_body"
            children += self.statements(statement)
        elif artefact_type == "inline_function":
            body = "table_function_body"
            attrs["form"] = "inline"
            select = return_type.SelectStatement
            try:
                plan, _ = self.select_statement(select)
            except Opaque as opaque:
                plan = self.opaque(select, opaque.reason)
            children.append(as_role(plan, "query"))
        else:
            body = "table_function_body"
            declared = return_type.DeclareTableVariableBody
            attrs.update(form="multi_statement", return_variable=str(declared.VariableName.Value))
            attrs["returns"] = [{"name": str(c.ColumnIdentifier.Value), "type": self.data_type(c.DataType).to_json()}
                                for c in declared.Definition.ColumnDefinitions]
            children += self.statements(statement)
        return Node(body, self.span(statement), None, attrs, children)

    def parameters(self, statement) -> list[Node]:
        declared = {p["name"].casefold(): p for p in self.reader.value(self.identity, "parameters")}
        out = []
        for p in statement.Parameters:
            name = str(p.VariableName.Value)
            fact = declared.get(name.casefold()) or declared.get("@" + name.casefold().lstrip("@"))
            if fact is None:
                raise ValueError(f"{self.identity}: parameter {name} is not in the catalog")
            t = T.from_column(fact, default=self.default_label)
            self.variables[name.casefold()] = (fact["name"], t)
            default = self.text_of(p.Value) if p.Value is not None else None
            attrs = {"name": fact["name"], "direction": fact["direction"], "readonly": fact["readonly"],
                     "default": default}
            out.append(Node("declare_parameter", self.span(p), t, attrs, role="parameter"))
        return out

    def statements(self, statement) -> list[Node]:
        listed = list(statement.StatementList.Statements) if statement.StatementList is not None else []
        if len(listed) == 1 and type_name(listed[0]) == "BeginEndBlockStatement":
            listed = list(listed[0].StatementList.Statements)
        return [as_role(self.body_statement(s), "statement") for s in listed]

    def body_statement(self, s) -> Node:
        kind = type_name(s)
        if kind == "ReturnStatement":
            children = []
            if s.Expression is not None:
                try:
                    value = self.expression(s.Expression)
                except Opaque as opaque:
                    value = self.opaque(s.Expression, opaque.reason)
                children.append(as_role(value, "argument"))
            return Node("return", self.span(s), None, {}, children)
        if kind == "ExecuteStatement":
            dynamic = self.dynamic_sql(s)
            if dynamic is not None:
                return dynamic
        if kind in PERMISSIONS:
            return Node("permission_change", self.span(s), None,
                        {"text": self.text_of(s), "manual": True, "scope_barrier": False})
        if kind in SCHEMA_CHANGES and not self.temp_target(s):
            return Node("schema_change", self.span(s), None,
                        {"text": self.text_of(s), "manual": True, "scope_barrier": False})
        return self.opaque(s, f"{kind}: {PROCEDURAL}", barrier=True)

    def temp_target(self, statement) -> bool:
        found = []

        def visit(f):
            if type_name(f) == "SchemaObjectName" and f.BaseIdentifier is not None \
                    and str(f.BaseIdentifier.Value).startswith("#"):
                found.append(f)
        walk(statement, visit)
        return bool(found)

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

    def lowered_or_opaque(self, fragment) -> Node:
        try:
            return self.expression(fragment)
        except Opaque as opaque:
            return self.opaque(fragment, opaque.reason)

    # ------------------------------------------------------------------ comments, hints, dependencies

    def attach_comments(self, body: Node) -> None:
        """A comment annotates the nearest node: the one that ends before it on its own line, if there is one
        (a comment after a select item), otherwise the next one (a comment on a line of its own)."""
        anchors = sorted((n for n in body.walk() if n.role in ("output", "input", "left", "right", "query", "statement",
                                                               "definition", "parameter")),
                         key=lambda n: n.position.offset)
        for comment in self.parsed.comments:
            line = self.parsed.text.count("\n", 0, comment.offset) + 1
            same_line = [n for n in anchors if n.position.end_line == line
                         and n.position.offset + n.position.length <= comment.offset]
            after = [n for n in anchors if n.position.offset >= comment.offset]
            chosen = max(same_line, key=lambda n: n.position.offset + n.position.length) if same_line else (
                after[0] if after else body)
            chosen.comments.append(self.source(comment.offset, len(comment.text)).rstrip())

    def hints(self, body: Node) -> list[dict]:
        """Every table, join and query hint on a node, for the report."""
        out = []
        for node in body.walk():
            for hint in list(node.attrs.get("hints", [])) + list(node.attrs.get("query_hints", [])):
                out.append({"node": node.id, "source_position": str(node.span), "hint": hint})
        return out

    def dependencies(self, body: Node) -> list[str]:
        found = set(self.opaque_dependencies)
        for n in body.walk():
            if n.kind == "read_table" and n.attrs.get("table"):
                found.add(n.attrs["table"])
            elif n.kind in ("call_table_function", "call_scalar_function"):
                found.add(n.attrs["function"])
        if self.reader.has_fact(self.identity, "references"):
            found |= {r["resolved"] for r in self.reader.value(self.identity, "references") if r.get("resolved")}
        found.discard(self.identity)
        return sorted(found)

    def opaque(self, fragment, reason: str, role: str = "", *, barrier: bool = False) -> Node:
        node = super().opaque(fragment, reason, role, barrier=barrier)
        self.opaque_dependencies |= self.referenced_objects(fragment)
        return node

    def referenced_objects(self, fragment) -> set[str]:
        """Catalog objects an opaque fragment names, so its dependencies are not lost."""
        found: set[str] = set()
        candidates: list[tuple[str, ...]] = []

        def visit(f):
            kind = type_name(f)
            if kind in ("NamedTableReference", "SchemaObjectFunctionTableReference") and f.SchemaObject is not None:
                candidates.append(tuple(str(i.Value) for i in f.SchemaObject.Identifiers))
            elif kind == "FunctionCall" and f.CallTarget is not None and type_name(f.CallTarget) == "MultiPartIdentifierCallTarget":
                candidates.append(tuple(str(i.Value) for i in f.CallTarget.MultiPartIdentifier.Identifiers) +
                                  (str(f.FunctionName.Value),))
        walk(fragment, visit)
        for parts in candidates:
            if not parts or len(parts) > 3 or "".join(parts[-1:]).startswith("#"):
                continue
            try:
                resolved = self.reader.resolve(parts, dict(self.name_context))
            except names.AmbiguousName:
                continue
            if resolved:
                found.add(resolved)
        return found


def lower_artefact(reader: CatalogReader, identity: str, parsed: Parsed) -> Artefact:
    """Lower a view or function (by its catalog kind) into the intermediate representation."""
    return Lowering(reader, identity, parsed).lower()



def columns_of(reader: CatalogReader) -> Callable[[str], set[str]]:
    """The column names the catalog holds for a table, view or function result, for the checker."""
    def names_of(identity: str) -> set[str]:
        fact = "columns" if reader.kind_of(identity) == "table" else "result_shape"
        return {c["name"] for c in reader.value(identity, fact)}
    return names_of

