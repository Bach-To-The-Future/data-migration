"""Lowering for SQL Server views, functions, procedures, triggers and batches (spec sections 4 and 5).

Deterministic and faithful: every node gets the vocabulary operation that means exactly what the
T-SQL means, typed from the catalog, with its source position (expressions.py, queries.py and
procedural.py hold the rules). What lowering cannot express is kept verbatim as an opaque fragment
with its reason, never dropped: the whole query when its shape is not handled, otherwise the one
select item or the one statement.

Artefacts handled here: a view (`view_body`, one query), an inline table-valued function
(`table_function_body`, one query), a scalar or multi-statement function, a procedure (`procedure_body`),
a trigger (`trigger_body`, with the table, timing and events the catalog holds) and a script of batches
(`batch_script_body`, one `batch` per GO). The statements of the last four are lowered by procedural.py;
what it does not lower is an opaque fragment, and dynamic SQL has its own node kind. Every statement carries its scope (core.scopes).
"""
from __future__ import annotations

from collections.abc import Callable

from ...core import scopes, vocabulary
from ...core.catalog import CatalogReader
from ...core.ir import Artefact, Node
from . import collations, names
from . import types as T
from .expressions import Opaque, as_role, type_name
from .parser import Parsed, walk
from .errors import Errors

VIEWS = {"CreateViewStatement", "CreateOrAlterViewStatement", "AlterViewStatement"}
FUNCTIONS = {"CreateFunctionStatement", "CreateOrAlterFunctionStatement", "AlterFunctionStatement"}
PROCEDURES = {"CreateProcedureStatement", "CreateOrAlterProcedureStatement", "AlterProcedureStatement"}
TRIGGERS = {"CreateTriggerStatement", "CreateOrAlterTriggerStatement", "AlterTriggerStatement"}
FUNCTION_TYPES = ("scalar_function", "inline_function", "table_function")


class Lowering(Errors):
    def __init__(self, reader: CatalogReader, identity: str, parsed: Parsed):
        super().__init__(reader, identity, parsed)
        self.opaque_dependencies: set[str] = set()

    # ------------------------------------------------------------------ artefact

    def lower(self) -> Artefact:
        artefact_type = self.reader.kind_of(self.identity)
        batches = list(self.parsed.tree.Batches)
        if artefact_type == "query_batch":
            body = self.batch_script(batches)
        else:
            [batch] = batches
            [statement] = list(batch.Statements)
            kind = type_name(statement)
            if kind in VIEWS and artefact_type == "view":
                body = self.view(statement)
            elif kind in FUNCTIONS and artefact_type in FUNCTION_TYPES:
                body = self.function(statement, artefact_type)
            elif kind in PROCEDURES and artefact_type == "procedure":
                body = self.procedure(statement)
            elif kind in TRIGGERS and artefact_type == "trigger":
                body = self.trigger(statement)
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
        scopes.annotate(body)
        artefact.hints = self.hints(body)
        return artefact

    def stored_settings(self, statement) -> dict:
        options = []
        if statement is not None:
            options = [self.text_of(o) for o in (getattr(statement, "ViewOptions", None)
                                                 or getattr(statement, "Options", None) or [])]
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
            self.return_type = T.from_column(self.reader.value(self.identity, "returns"), default=self.default_label)
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
            self.declare_return_variable(declared)
            children += self.statements(statement)
        return Node(body, self.span(statement), None, attrs, children)

    def declare_return_variable(self, declared) -> None:
        """The row set `RETURNS @t TABLE (...)` declares is a table variable of the function's body."""
        label = collations.Label(self.reader.value(self.database, "collation"), collations.COLUMN)
        _, types, _ = self.table_definition(declared.Definition, label)
        name = str(declared.VariableName.Value)
        self.row_sets[name.casefold()] = (name, types)

    def parameters(self, statement) -> list[Node]:
        declared = {p["name"].casefold(): p for p in self.reader.value(self.identity, "parameters")}
        out = []
        for p in statement.Parameters:
            name = str(p.VariableName.Value)
            fact = declared.get(name.casefold()) or declared.get("@" + name.casefold().lstrip("@"))
            if fact is None:
                raise ValueError(f"{self.identity}: parameter {name} is not in the catalog")
            t = T.from_column(fact, default=self.default_label)
            default = self.text_of(p.Value) if p.Value is not None else None
            attrs = {"name": fact["name"], "direction": fact["direction"], "readonly": fact["readonly"],
                     "default": default}
            if fact["system_type"] == "table":
                attrs["row_set"] = True               # a table-valued parameter is read as a table variable is
                self.row_sets[fact["name"].casefold()] = (fact["name"], self.table_type_columns(fact["declared_type"]))
            else:
                self.variables[name.casefold()] = (fact["name"], t)
            out.append(Node("declare_parameter", self.span(p), t, attrs, role="parameter"))
        return out

    def table_type_columns(self, declared_type: str) -> dict:
        found = [o for o in self.reader.snapshot.objects if o.kind == "table_type"
                 and o.identity.removeprefix("type:").split(".")[0] == self.database
                 and o.identity.split(".")[-1].casefold() == declared_type.casefold()]
        if len(found) != 1:
            raise ValueError(f"{self.identity}: the table type {declared_type} of a parameter is not in the catalog "
                             f"exactly once ({len(found)} found)")
        return {c["name"]: T.from_column(c, default=self.default_label) for c in found[0].fact("columns").value}

    def statements(self, statement) -> list[Node]:
        listed = list(statement.StatementList.Statements) if statement.StatementList is not None else []
        return self.statement_list(listed, "statement")

    # ------------------------------------------------------------------ procedures, triggers, batches

    def procedure(self, statement) -> Node:
        children = self.parameters(statement) + self.statements(statement)
        return Node("procedure_body", self.span(statement), None, dict(self.stored_settings(statement), name=self.identity),
                    children)

    def trigger(self, statement) -> Node:
        table = str(self.reader.value(self.identity, "table"))
        self.trigger_table = table
        attrs = dict(self.stored_settings(statement), name=self.identity, table=table,
                     timing=self.reader.value(self.identity, "timing"),
                     events=list(self.reader.value(self.identity, "events")))
        return Node("trigger_body", self.span(statement), None, attrs, self.statements(statement))

    def batch_script(self, batches) -> Node:
        """One `batch` per GO. Variables and table variables end with their batch; temp tables do not."""
        out = []
        for batch in batches:
            self.begin_batch()
            statements = self.statement_list(list(batch.Statements), "statement")
            out.append(Node("batch", self.span(batch), None, {}, statements, role="batch"))
        return Node("batch_script_body", self.parsed.span(self.parsed.tree), None,
                    dict(self.stored_settings(None), name=self.identity), out)

    # ------------------------------------------------------------------ comments, hints, dependencies

    def attach_comments(self, body: Node) -> None:
        """A comment annotates the nearest node: the one that ends before it on its own line, if there is one
        (a comment after a select item), otherwise the next one (a comment on a line of its own)."""
        roles = ("output", "input", "left", "right", "query", "definition", "parameter", "batch") + scopes.STATEMENT_ROLES
        anchors = sorted((n for n in body.walk() if n.role in roles), key=lambda n: n.position.offset)
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
        if body.kind == "trigger_body":
            found.add(body.attrs["table"])
        for n in body.walk():
            if n.kind == "read_table" and n.attrs.get("table"):
                found.add(n.attrs["table"])
            elif n.kind in ("call_table_function", "call_scalar_function"):
                found.add(n.attrs["function"])
            elif n.kind == "call_procedure":
                found.add(n.attrs["procedure"])
            elif n.kind in ("insert_rows", "update_rows", "delete_rows") and n.attrs["target_kind"] == "table":
                found.add(n.attrs["target"])
            elif n.kind == "write_current_row":
                found.add(n.attrs["target"])
            elif n.kind in ("last_identity", "set_session_option") and n.attrs.get("table"):
                found.add(n.attrs["table"])
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
    """Lower a view, function, procedure, trigger or batch script (by its catalog kind) into the representation."""
    return Lowering(reader, identity, parsed).lower()


def columns_of(reader: CatalogReader) -> Callable[[str], set[str]]:
    """The column names the catalog holds for a table, view or function result, for the checker."""
    def names_of(identity: str) -> set[str]:
        fact = "columns" if reader.kind_of(identity) == "table" else "result_shape"
        return {c["name"] for c in reader.value(identity, fact)}
    return names_of
