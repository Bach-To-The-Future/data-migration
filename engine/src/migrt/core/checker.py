"""The checker (spec section 4, item 8): runs after every step that creates or changes the
intermediate representation, whoever made the change.

It verifies that every node is in the vocabulary and has a source position, that every name
resolves to the catalog or to something in scope, that types agree, and that every opaque
fragment is accounted for. It checks the shape of the plan and the types of the operations
lowering produces for views and functions, and the procedural part (issue #43): every statement's scope is
derived again and compared with the one lowering wrote, every variable, table variable and temp table a node
reads is in the scope of its statement, every jump has its label, and every opaque statement either says what it
touches or is a scope barrier. Cursors, transactions, errors and messages (issue #85): every cursor a statement
names is in scope, a fetch names variables in scope and as many as the cursor has columns, a rollback to a savepoint
names one that may be set, and the values these statements read (the row count, the error number, the transaction
depth, an error field, the identity value) have their types.

A part of a divided artefact (issue #86, core.divider) is checked as an artefact is: its scopes are derived from the
state it starts in, and what lies outside it comes from what the divider carried: the types of the variables in scope,
the column counts of the cursors, every label, and whether it lies inside a loop.
"""
from __future__ import annotations

import dataclasses
from typing import Callable

from . import scopes, vocabulary
from .ir import Artefact, Node, VType

# Operations whose operands must be of one type family.
INTEGER_OPERANDS = {"integer_divide"}
MONEY_OPERAND = {"money_multiply", "money_divide"}
VALUE_KINDS = {"column", "literal", "null", "variable"}
# Plan steps and what each needs (roles of its children).
PLAN_ROLES = {
    "filter": {"input", "condition"}, "project": {"input"}, "aggregate": {"input"}, "distinct": {"input"},
    "sort": {"input", "order"}, "limit": {"input", "count"}, "skip_and_limit": {"input", "skip"},
    "join": {"left", "right"}, "lateral_join": {"left", "right"}, "union": {"left", "right"},
    "intersect": {"left", "right"}, "except": {"left", "right"}, "named_query": {"query"},
    "pivot": {"input", "argument", "key"}, "shred_xml": {"argument"}, "exists": {"query"}, "scalar_query": {"query"},
    "in_query": {"argument", "query"}, "window": {"function"},
    "if": {"condition"}, "while": {"condition"}, "assign": {"argument"}, "assign_from_rows": {"query"},
    "emit_result_set": {"query"}, "insert_rows": {"source"}, "update_rows": {"input"}, "delete_rows": {"input"},
    "declare_cursor": {"query"}, "emit_message": {"argument"},
}
# Reads of session state: each must say its type.
TYPED_READS = {"rows_affected", "last_statement_error", "transaction_depth", "transaction_state", "fetch_status",
               "error_field", "last_identity"}
CURSOR_STATEMENTS = ("open_cursor", "fetch_cursor", "close_cursor", "deallocate_cursor", "write_current_row")
RAISE_FORMS = {"raiserror": {"message", "severity", "state"}, "throw": {"number", "message", "state"}}
# Operations that compare strings by a comparison rule, and where the rule goes.
RULED = {"compare", "between", "in_list", "like", "null_if_equal", "minimum", "maximum", "position_of",
         "replace_text", "in_query"}
OUTPUT = "(output)"       # the relation a sort key reads a select-list column through
SOURCES = ("read_table", "named_query", "call_table_function", "read_values", "shred_xml", "pivot",
           "external_reference", "read_changed_rows")
CALLS = ("call_table_function", "call_scalar_function", "call_procedure")


@dataclasses.dataclass(frozen=True)
class Finding:
    node_id: str
    message: str


class CheckFailed(Exception):
    def __init__(self, identity: str, findings: list[Finding]):
        self.findings = findings
        super().__init__(f"{identity} failed the checker: " + "; ".join(f"{f.node_id}: {f.message}" for f in findings))


def _family(node: Node) -> tuple:
    """What must agree between operands: the type's name, with the width of an integer and the unit of a string."""
    t = node.type
    if t is None:
        return ("?",)
    if t.name == "integer":
        return ("integer", t.param("bits"))
    if t.name == "string":
        return ("string", t.param("unit"))
    if t.name == "float":
        return ("float", t.param("bits"))
    return (t.name,)


def _family_of(t) -> tuple:
    return _family(Node("type", None, t))


def plan_columns(node: Node) -> list[str] | None:
    """The names of the columns a query plan returns, from its select block (the left query of a set operation)."""
    while node.kind not in ("project", "aggregate"):
        if node.kind in ("union", "intersect", "except"):
            node = node.child("left")
        elif node.children_with("input"):
            node = node.child("input")
        else:
            return None
    return [o.attrs["output_name"] for o in node.children_with("output")]


def source_columns(source: Node, definitions: dict[str, Node]) -> list[str] | None:
    """The columns a row source offers, where the plan says: None for one it cannot know (an external reference)."""
    if source.kind == "named_query" and source.children_with("query"):
        return plan_columns(source.child("query"))
    if source.kind == "read_table" and source.attrs.get("named_query"):
        definition = definitions.get(source.attrs["named_query"])
        return plan_columns(definition.child("query")) if definition else None
    if source.kind == "shred_xml":
        return [source.attrs["column"]]
    return source.attrs.get("columns")


def check(artefact: Artefact, columns_of: Callable[[str], set[str]]) -> list[Finding]:
    """columns_of(table identity) gives the column names the catalog holds for it."""
    findings: list[Finding] = []

    def fail(node: Node, message: str) -> None:
        findings.append(Finding(node.id, message))

    # An alias can name different sources in different queries of one artefact.
    aliases: dict[str, list[Node]] = {}
    for n in artefact.nodes():
        if n.kind in SOURCES and n.attrs.get("alias"):
            aliases.setdefault(n.attrs["alias"], []).append(n)
        elif n.kind == "named_query":
            aliases.setdefault(n.attrs["name"], []).append(n)
    definitions = {n.attrs["name"]: n for n in artefact.nodes() if n.kind == "named_query" and n.role == "definition"}
    parameters = {n.attrs["name"].casefold() for n in artefact.nodes() if n.kind == "declare_parameter"}
    declared_types = {n.attrs["name"].casefold(): n.type for n in artefact.nodes()
                      if n.kind in ("declare_parameter", "declare_variable") and n.type is not None}
    labels = {n.attrs["name"].casefold() for n in artefact.nodes() if n.kind == "declare_label"}
    cursor_columns: dict[str, list[int | None]] = {}
    in_loop: set[int] = set()
    if artefact.body.kind == "part_body":        # a part of a divided artefact brings what lies outside it
        carried = artefact.body.attrs
        labels |= {name.casefold() for name in carried["labels"]}
        for declaration in carried["declarations"]:
            if declaration["type"] is not None and declaration["group"] in ("parameters", "variables"):
                declared_types[declaration["name"].casefold()] = VType.from_json(declaration["type"])
            if declaration["group"] == "cursors":
                cursor_columns.setdefault(declaration["name"].casefold(), []).append(declaration["columns"])
        if any(entry["kind"] == "while" for entry in carried["path"]):
            in_loop |= {id(n) for n in artefact.nodes()}
    for n in artefact.nodes():
        if n.kind == "declare_cursor":
            cursor_columns.setdefault(n.attrs["name"].casefold(), []).append(
                None if n.attrs["columns"] is None else len(n.attrs["columns"]))
    for loop in (n for n in artefact.nodes() if n.kind == "while"):
        in_loop |= {id(n) for n in loop.walk()}
    scope_for: dict[int, dict] = {}
    for statement, scope in scopes.derive(artefact.body):
        if statement.attrs.get("scope") != scope:
            fail(statement, "its recorded scope differs from the scope derived from the statements before it")
        for inner in statement.walk():
            scope_for[id(inner)] = scope           # a nested statement is derived after, and takes its own

    expected_stale = {id(n): barrier for n, barrier in scopes.stale(artefact.body)}
    for n in artefact.nodes():
        flag = n.attrs.get("scope_may_be_stale")
        if id(n) in expected_stale and flag != expected_stale[id(n)]:
            fail(n, f"{n.kind} depends on a scope entry that may be stale after the scope barrier at "
                    f"{expected_stale[id(n)]}, and does not carry the flag")
        elif id(n) not in expected_stale and flag is not None:
            fail(n, f"{n.kind} carries the may-be-stale flag, but no scope barrier comes before it or it depends on "
                    "no scope entry")

    def in_scope(node: Node, name: str, *, row_set: bool = False) -> bool:
        scope = scope_for.get(id(node))
        if scope is None:                        # a view or an inline function: only its parameters exist
            return not row_set and name.casefold() in parameters
        return scopes.known(scope, name, row_set=row_set)

    def temp_in_scope(node: Node, name: str) -> bool:
        scope = scope_for.get(id(node))
        return scope is not None and scopes.temp_table_known(scope, name)

    for node in artefact.nodes():
        if not vocabulary.is_known(node.kind):
            fail(node, f"{node.kind} is not in the operation vocabulary")
        if node.span is None:
            fail(node, "has no source position")
        if node.kind == "opaque_fragment" and node.role in scopes.STATEMENT_ROLES and not node.attrs.get("scope_barrier"):
            touches = node.attrs.get("touches")
            if touches is None:
                fail(node, "an opaque statement that is not a scope barrier must say what it touches")
            else:
                undeclared = {n.casefold() for n in touches.get("undeclared", [])}
                for name in touches["variables"]:
                    if name.casefold() not in undeclared and not in_scope(node, name) \
                            and not in_scope(node, name, row_set=True):
                        fail(node, f"touches {name}, which is not in scope")
                for name in touches["temp_tables"]:
                    if name.casefold() not in undeclared and not temp_in_scope(node, name):
                        fail(node, f"touches {name}, which is not in scope")
        if node.kind in ("opaque_fragment", "dynamic_sql_execution", "schema_change", "permission_change"):
            if node.kind == "opaque_fragment" and (not node.attrs.get("reason") or not node.attrs.get("text")):
                fail(node, "an opaque fragment needs its reason and its verbatim text")
            if not isinstance(node.attrs.get("scope_barrier"), bool):
                fail(node, f"{node.kind} must say whether it is a scope barrier")
            if node.kind != "opaque_fragment" and node.attrs.get("manual") is not True:
                fail(node, f"{node.kind} is always manual work")
            continue
        missing = PLAN_ROLES.get(node.kind, set()) - {c.role for c in node.children}
        if missing:
            fail(node, f"{node.kind} lacks {sorted(missing)}")
        if node.kind == "read_table":
            if node.attrs.get("named_query"):
                if node.attrs["named_query"] not in {n.attrs["name"] for n in artefact.nodes() if n.kind == "named_query"}:
                    fail(node, f"reads the named query {node.attrs['named_query']}, which is not defined")
            elif node.attrs.get("temp_table"):
                if not temp_in_scope(node, node.attrs["temp_table"]):
                    fail(node, f"reads the temp table {node.attrs['temp_table']}, which is not in scope")
            elif node.attrs.get("row_set"):
                if not in_scope(node, node.attrs["row_set"], row_set=True):
                    fail(node, f"reads the table variable {node.attrs['row_set']}, which is not in scope")
            elif node.attrs["table"] not in artefact.dependencies:
                fail(node, f"reads {node.attrs['table']}, which is not among the dependencies")
        if node.kind in ("insert_rows", "update_rows", "delete_rows"):
            target, target_kind = node.attrs["target"], node.attrs["target_kind"]
            if target_kind == "temp_table" and not temp_in_scope(node, target):
                fail(node, f"writes the temp table {target}, which is not in scope")
            elif target_kind == "row_set" and not in_scope(node, target, row_set=True):
                fail(node, f"writes the table variable {target}, which is not in scope")
            elif target_kind == "table" and target not in artefact.dependencies:
                fail(node, f"writes {target}, which is not among the dependencies")
        if node.kind in ("write_current_row", "last_identity", "set_session_option"):
            named = node.attrs.get("target" if node.kind == "write_current_row" else "table")
            if named is not None and named not in artefact.dependencies:
                fail(node, f"names {named}, which is not among the dependencies")
        if node.kind in CALLS:
            called = node.attrs["procedure"] if node.kind == "call_procedure" else node.attrs["function"]
            if called not in artefact.dependencies:
                fail(node, f"calls {called}, which is not among the dependencies")
        if node.kind == "variable" and not in_scope(node, node.attrs["name"]):
            fail(node, f"{node.attrs['name']} is not in scope: it is neither a parameter nor a variable declared "
                       "before this point")
        if node.kind in CURSOR_STATEMENTS:
            scope = scope_for.get(id(node))
            name = node.attrs["cursor"]
            if scope is not None and not scopes.cursor_known(scope, name):
                fail(node, f"names the cursor {name}, which is not in scope: no cursor of that name is declared before "
                           "this point")
            if node.kind == "fetch_cursor":
                counts = cursor_columns.get(name.casefold(), [])
                if counts and None not in counts and len(node.attrs["variables"]) not in counts:
                    fail(node, f"fetches into {len(node.attrs['variables'])} variables from a cursor of "
                               f"{counts} columns")
                if len(node.attrs["targets"]) != len(node.attrs["variables"]):
                    fail(node, "names a different number of conversions than variables")
        if node.kind == "rollback_to_save_point":
            scope = scope_for.get(id(node))
            if scope is not None and not scope["barrier"] and not any(
                    p.casefold() == node.attrs["save_point"].casefold() for p in scope["transaction"]["save_points"]):
                fail(node, f"rolls back to the savepoint {node.attrs['save_point']}, which is not set before this point")
        if node.kind in TYPED_READS and (node.type is None or node.type.name == "unknown"):
            fail(node, f"{node.kind} reads session state and says no type")
        if node.kind == "emit_message" and node.children_with("argument"):
            message = node.child("argument")
            if message.kind != "opaque_fragment" and message.type is not None and message.type.name != "string":
                fail(node, "sends a message that is not text, without converting it")
        if node.kind == "raise_error":
            form = node.attrs.get("form")
            if form not in RAISE_FORMS:
                fail(node, f"raises an error in the form {form!r}, which is neither raiserror nor throw")
            elif RAISE_FORMS[form] - {c.role for c in node.children}:
                fail(node, f"{form} lacks {sorted(RAISE_FORMS[form] - {c.role for c in node.children})}")
        written: list[str] = []
        if node.kind == "assign":
            written = [node.attrs["variable"]]
        elif node.kind in ("assign_from_rows", "fetch_cursor"):
            written = list(node.attrs["variables"])
        elif node.kind == "call_procedure" and node.attrs.get("return_variable"):
            written = [node.attrs["return_variable"]]
        for name in written:
            if not in_scope(node, name):
                fail(node, f"assigns {name}, which is not in scope")
        if node.kind in ("break", "continue") and id(node) not in in_loop:
            fail(node, f"{node.kind} is outside every loop")
        if node.kind == "jump" and node.attrs["label"].casefold() not in labels:
            fail(node, f"jumps to the label {node.attrs['label']}, which is not declared")
        if node.kind in ("assign", "declare_variable"):
            value = node.children_with("argument" if node.kind == "assign" else "initial")
            variable = node.attrs["variable"] if node.kind == "assign" else node.attrs["name"]
            target_type = declared_types.get(variable.casefold())
            if value and target_type is not None and value[0].kind not in ("opaque_fragment", "null") \
                    and value[0].type is not None and _family(value[0]) != _family_of(target_type):
                fail(node, f"stores a {value[0].type} value in a {target_type} variable without converting it")
        if node.kind == "column":
            relation = node.attrs["relation"]
            if relation == OUTPUT:
                pass                                  # checked at the sort that reads it
            elif relation not in aliases:
                fail(node, f"relation {relation} is not in scope")
            elif node.attrs.get("table") is None:
                offered = [source_columns(s, definitions) for s in aliases[relation]]
                if None not in offered and not any(node.attrs["column"].casefold() in {c.casefold() for c in cols}
                                                   for cols in offered if cols is not None):
                    fail(node, f"{relation} has no column {node.attrs['column']}")
            elif node.attrs.get("table") is not None:
                if node.attrs["table"] not in {s.attrs.get("table") for s in aliases[relation]}:
                    fail(node, f"relation {relation} is not in scope as {node.attrs['table']}")
                elif node.attrs["column"] not in columns_of(node.attrs["table"]):
                    fail(node, f"{node.attrs['table']} has no column {node.attrs['column']}")
        if vocabulary.part_of(node.kind) == "expression" or node.kind in VALUE_KINDS:
            if node.type is None or node.type.name == "unknown":
                fail(node, "an expression without an established type")
        arguments = node.children_with("argument")
        operand_types = [c.type.name for c in arguments if c.type is not None]
        if node.kind in INTEGER_OPERANDS and operand_types != ["integer", "integer"]:
            fail(node, f"{node.kind} needs two integer operands, not {operand_types}")
        if node.kind in MONEY_OPERAND and "money" not in operand_types:
            fail(node, f"{node.kind} needs a money operand, not {operand_types}")
        if node.kind in ("add", "subtract", "multiply", "modulo", "decimal_divide", "float_divide", "money_divide",
                         "money_multiply", "integer_divide", "compare", "null_replacement"):
            if len({_family(c) for c in arguments}) > 1 and not any(c.kind == "null" for c in arguments):
                fail(node, f"operand types do not agree: {[str(c.type) for c in arguments]}")
        if node.kind in ("between", "in_list"):
            members = arguments + node.children_with("item")
            if len({_family(c) for c in members}) > 1:
                fail(node, f"operand types do not agree: {[str(c.type) for c in members]}")
        if node.kind == "case":
            values = node.children_with("then") + node.children_with("else")
            if len({_family(c) for c in values if c.kind != "null"}) > 1:
                fail(node, f"branch types do not agree: {[str(c.type) for c in values]}")
        if node.kind == "first_non_null" and len({_family(c) for c in arguments if c.kind != "null"}) > 1:
            fail(node, f"operand types do not agree: {[str(c.type) for c in arguments]}")
        if node.kind in RULED:
            strings = [c for c in arguments + node.children_with("item") if c.type is not None and c.type.name == "string"]
            rule = node.attrs.get("rule")
            if strings and not rule:
                fail(node, f"{node.kind} compares strings and names no comparison rule")
            if rule is not None and not strings:
                fail(node, f"{node.kind} names a comparison rule but compares no strings")
            if rule is not None and not rule.get("collation"):
                fail(node, f"{node.kind}'s comparison rule names no collation")
        if node.kind == "sort":
            offered = plan_columns(node.child("input"))
            for key in node.children_with("order"):
                for used in key.walk():
                    if used.kind == "column" and used.attrs["relation"] == OUTPUT and offered is not None                             and used.attrs["column"] not in offered:
                        fail(used, f"the select list has no column {used.attrs['column']}")
        for child in node.children:
            if child.role in ("order", "partition", "key") and child.type is not None and child.type.name == "string"                     and not child.attrs.get("rule"):
                fail(child, f"a string {child.role} key of {node.kind} names no comparison rule")
        if node.kind in ("and", "or", "not") and any(c.type is None or c.type.name != "boolean" for c in arguments):
            fail(node, f"{node.kind} needs boolean operands")
    # A result set sent to the caller, the rows an INSERT reads and the rows EXISTS tests (`SELECT *` over a join
    # repeats names) may repeat a column name; a view, a derived table or a named query may not.
    repeating = set()
    for node in artefact.nodes():
        if node.kind in ("emit_result_set", "assign_from_rows", "exists", "insert_rows"):
            found = node.children_with("source" if node.kind == "insert_rows" else "query")
            if not found:
                continue
            plan = found[0]
            while plan.kind in ("union", "intersect", "except") or (
                    plan.kind not in ("project", "aggregate") and plan.children_with("input")):
                plan = plan.child("left" if plan.kind in ("union", "intersect", "except") else "input")
            repeating.add(id(plan))
    for node in artefact.nodes():
        outputs = [c.attrs["output_name"] for c in node.children_with("output")]
        if id(node) not in repeating and len(outputs) != len({o.casefold() for o in outputs}):
            fail(node, f"output names are not unique: {outputs}")
    return findings


def require(artefact: Artefact, columns_of: Callable[[str], set[str]]) -> None:
    findings = check(artefact, columns_of)
    if findings:
        raise CheckFailed(artefact.identity, findings)
