"""Lowering of cursors (spec sections 4 and 5, issue #85). A cursor is lowered as a cursor: raising it to a set-based
step is optional and comes later.

`DECLARE c CURSOR ... FOR select` is `declare_cursor` (the select is its `query`; `columns` name and type what it
returns; `cursor_scope`, `kind`, `scrollable`, `read_only` and the other options say what the declaration says, and a
cursor declared with neither LOCAL nor GLOBAL takes the database's default, recorded as an assumption). `OPEN`, `CLOSE`
and `DEALLOCATE` name the cursor; `FETCH` names the cursor, the direction (with an `offset` child for `ABSOLUTE` and
`RELATIVE`) and the variables, with the type each column has and the type each variable has (`targets`).
`UPDATE` and `DELETE ... WHERE CURRENT OF` are `write_current_row`. `@@FETCH_STATUS` is `fetch_status`.

A cursor named with `GLOBAL`, a cursor held in a variable, a cursor that was not declared before the statement in its
batch and a fetch into variables that are not in scope or are not as many as the cursor's columns are kept as written
with the reason. A statement that names no cursor can still change one, so a cursor statement kept as written is a
scope barrier, except a `FETCH`, which can change nothing but the variables it names."""
from __future__ import annotations

from ...core.ir import Node, VType
from .expressions import Opaque, Relation, as_role, type_name
from .transactions import Transactions

KINDS = {"Static": "static", "Keyset": "keyset", "Dynamic": "dynamic", "FastForward": "fast_forward",
         "Insensitive": "static"}
ORIENTATIONS = {"Next": "next", "Prior": "prior", "First": "first", "Last": "last", "Absolute": "absolute",
                "Relative": "relative"}
DEFAULT_ASSUMPTION = ("A cursor declared with neither LOCAL nor GLOBAL takes the database's CURSOR_DEFAULT option, which "
                      "the catalog does not hold: it is assumed GLOBAL, SQL Server's default.")


class Cursors(Transactions):
    def __init__(self, *args):
        super().__init__(*args)
        # cursors declared so far in the batch: folded name -> (name, what it returns, whether it outlives the batch).
        # What it returns is a list of (name, type): the type is None, or the whole list is, where the query was kept
        # as written and what it returns is not known.
        self.cursors: dict[str, tuple[str, list[tuple[str | None, VType | None]] | None, bool]] = {}

    # ------------------------------------------------------------------ declaring

    def s_DeclareCursorStatement(self, s) -> list[Node]:
        name = str(s.Name.Value)
        definition = s.CursorDefinition
        options = {str(o.OptionKind) for o in definition.Options}
        scope = "local" if "Local" in options else "global" if "Global" in options else "database_default"
        stated = [KINDS[o] for o in options if o in KINDS and o != "Insensitive"]
        if len(stated) > 1:
            raise Opaque(s, "a cursor declared with more than one of STATIC, KEYSET, DYNAMIC and FAST_FORWARD")
        kind = stated[0] if stated else ("static" if "Insensitive" in options else "dynamic")
        if "Scroll" in options:
            scrollable = True
        elif "ForwardOnly" in options or "FastForward" in options:
            scrollable = False
        else:
            scrollable = bool(stated)                      # STATIC, KEYSET and DYNAMIC scroll unless told not to
        read_only = bool({"ReadOnly", "FastForward", "Insensitive", "Static"} & options)
        outputs: list[tuple[str | None, VType | None]] | None
        try:
            plan, found = self.select_statement(definition.Select)
            outputs = list(found)
        except Opaque as opaque:           # the cursor is still a cursor; its query is manual work
            plan, outputs = self.opaque(definition.Select, opaque.reason), self.written_columns(definition.Select)
        if scope == "database_default":
            self.assumptions.add(DEFAULT_ASSUMPTION)
        columns = None if outputs is None else [{"name": n, "type": t.to_json() if t is not None else None}
                                                for n, t in outputs]
        attrs = {"name": name, "cursor_scope": scope, "kind": kind, "scrollable": scrollable, "read_only": read_only,
                 "scroll_locks": "ScrollLocks" in options, "optimistic": "Optimistic" in options,
                 "type_warning": "TypeWarning" in options, "columns": columns,
                 "for_update": self.update_columns(definition.Select)}
        self.cursors[name.casefold()] = (name, outputs, scope != "local")
        return [Node("declare_cursor", self.span(s), None, attrs, [as_role(plan, "query")])]

    def update_columns(self, select) -> list[str] | None:
        """The columns `FOR UPDATE OF` names: None if there is no FOR UPDATE, an empty list for all of them."""
        clause = getattr(select.QueryExpression, "ForClause", None)
        if clause is None or type_name(clause) != "UpdateForClause":
            return None
        return [str(list(c.MultiPartIdentifier.Identifiers)[-1].Value) for c in clause.Columns]

    def written_columns(self, select) -> list[tuple[str | None, VType | None]] | None:
        """The columns a query returns as far as its text says, for a cursor whose query was kept as written: one for
        each select item, named by its alias or its column, with no type. None if the text does not say how many
        (a `*`, a set operation)."""
        spec = select.QueryExpression
        if type_name(spec) != "QuerySpecification" or any(type_name(e) != "SelectScalarExpression"
                                                           for e in spec.SelectElements):
            return None
        out: list[tuple[str | None, VType | None]] = []
        for element in spec.SelectElements:
            if element.ColumnName is not None:
                name = str(element.ColumnName.Value)
            elif type_name(element.Expression) == "ColumnReferenceExpression":
                name = str(list(element.Expression.MultiPartIdentifier.Identifiers)[-1].Value)
            else:
                name = None
            out.append((name, None))
        return out

    def begin_batch(self) -> None:
        """A local cursor ends with its batch; a global one does not."""
        super().begin_batch()
        self.cursors = {k: v for k, v in self.cursors.items() if v[2]}

    # ------------------------------------------------------------------ naming a cursor

    def cursor_name(self, cursor, statement) -> str:
        """The declared name of the cursor a statement refers to."""
        if cursor.IsGlobal:
            raise Opaque(statement, "a reference to a cursor with GLOBAL is not lowered yet")
        if cursor.Name.Identifier is None:
            raise Opaque(statement, "a cursor held in a variable is not lowered yet (cursor variables are not lowered)")
        found = self.cursors.get(str(cursor.Name.Value).casefold())
        if found is None:
            raise Opaque(statement, f"cursor {cursor.Name.Value} is not declared before this point in its batch")
        return found[0]

    def s_OpenCursorStatement(self, s) -> list[Node]:
        return [Node("open_cursor", self.span(s), None, {"cursor": self.cursor_name(s.Cursor, s)})]

    def s_CloseCursorStatement(self, s) -> list[Node]:
        return [Node("close_cursor", self.span(s), None, {"cursor": self.cursor_name(s.Cursor, s)})]

    def s_DeallocateCursorStatement(self, s) -> list[Node]:
        name = self.cursor_name(s.Cursor, s)
        node = Node("deallocate_cursor", self.span(s), None, {"cursor": name})
        self.cursors.pop(name.casefold(), None)
        return [node]

    # ------------------------------------------------------------------ fetching

    def s_FetchCursorStatement(self, s) -> list[Node]:
        name = self.cursor_name(s.Cursor, s)
        columns = self.cursors[name.casefold()][1]
        fetch = s.FetchType
        orientation = ORIENTATIONS[str(fetch.Orientation)] if fetch is not None else "next"
        children = []
        if fetch is not None and fetch.RowOffset is not None:
            children.append(as_role(self.lowered_or_opaque(fetch.RowOffset), "offset"))
        variables, targets = [], []
        into = list(s.IntoVariables)
        missing = [str(v.Name) for v in into if str(v.Name).casefold() not in self.variables]
        if missing:
            raise Opaque(s, f"{', '.join(missing)} is not a parameter or a variable declared before this point in its "
                            "batch")
        if columns is not None and len(into) != len(columns):
            raise Opaque(s, f"FETCH INTO {len(into)} variables from a cursor of {len(columns)} columns")
        for i, variable in enumerate(into):
            declared, variable_type = self.variables[str(variable.Name).casefold()]
            column_type = columns[i][1] if columns is not None else None    # None: the query was kept as written
            variables.append(declared)
            targets.append({"variable": declared, "from_type": column_type.to_json() if column_type is not None else None,
                            "to_type": variable_type.to_json()})
        return [Node("fetch_cursor", self.span(s), None,
                     {"cursor": name, "direction": orientation, "variables": variables, "targets": targets}, children)]

    # ------------------------------------------------------------------ writing the current row

    def s_UpdateStatement(self, s) -> list[Node]:
        spec = s.UpdateSpecification
        if spec.WhereClause is not None and spec.WhereClause.Cursor is not None:
            return [self.write_current_row(s, spec, "update")]
        return super().s_UpdateStatement(s)

    def s_DeleteStatement(self, s) -> list[Node]:
        spec = s.DeleteSpecification
        if spec.WhereClause is not None and spec.WhereClause.Cursor is not None:
            return [self.write_current_row(s, spec, "delete")]
        return super().s_DeleteStatement(s)

    def write_current_row(self, s, spec, action: str) -> Node:
        name = self.cursor_name(spec.WhereClause.Cursor, s)
        if spec.FromClause is not None or spec.OutputClause is not None or spec.OutputIntoClause is not None \
                or spec.TopRowFilter is not None or s.WithCtesAndXmlNamespaces is not None:
            raise Opaque(s, f"{action.upper()} ... WHERE CURRENT OF with a FROM, OUTPUT, TOP or WITH clause is not lowered "
                            "yet")
        self.scopes.append({})
        try:
            target = self.table_reference(spec.Target)
            if target.kind != "read_table" or not target.attrs.get("table") or target.attrs.get("source") == "view":
                raise Opaque(s, "the target of a write through a cursor is not a table of the catalog")
            relation: Relation = self.scopes[-1][self.fold(target.attrs["alias"])]
            values = self.set_values(spec, relation) if action == "update" else []
        finally:
            self.scopes.pop()
        return Node("write_current_row", self.span(s), None,
                    {"cursor": name, "action": action, "target": target.attrs["table"], "columns": [c for c, _ in values]},
                    [v for _, v in values])
