"""The checker (spec section 4, item 8): runs after every step that creates or changes the
intermediate representation, whoever made the change.

It verifies that every node is in the vocabulary and has a source position, that every name
resolves to the catalog or to something in scope, that types agree, and that every opaque
fragment is accounted for. It checks the shape of the plan and the types of the operations
lowering produces for views and functions; later tickets extend it to the procedural part.
"""
from __future__ import annotations

import dataclasses
from typing import Callable

from . import vocabulary
from .ir import Artefact, Node

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
}
# Operations that compare strings by a comparison rule, and where the rule goes.
RULED = {"compare", "between", "in_list", "like", "null_if_equal", "minimum", "maximum", "position_of",
         "replace_text", "in_query"}
OUTPUT = "(output)"       # the relation a sort key reads a select-list column through
SOURCES = ("read_table", "named_query", "call_table_function", "read_values", "shred_xml", "pivot",
           "external_reference")
CALLS = ("call_table_function", "call_scalar_function")


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
    for node in artefact.nodes():
        if not vocabulary.is_known(node.kind):
            fail(node, f"{node.kind} is not in the operation vocabulary")
        if node.span is None:
            fail(node, "has no source position")
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
            elif node.attrs["table"] not in artefact.dependencies:
                fail(node, f"reads {node.attrs['table']}, which is not among the dependencies")
        if node.kind in CALLS and node.attrs["function"] not in artefact.dependencies:
            fail(node, f"calls {node.attrs['function']}, which is not among the dependencies")
        if node.kind == "variable" and node.attrs["name"].casefold() not in parameters:
            fail(node, f"{node.attrs['name']} is not a declared parameter")
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
    for node in artefact.nodes():
        outputs = [c.attrs["output_name"] for c in node.children_with("output")]
        if len(outputs) != len({o.casefold() for o in outputs}):
            fail(node, f"output names are not unique: {outputs}")
    return findings


def require(artefact: Artefact, columns_of: Callable[[str], set[str]]) -> None:
    findings = check(artefact, columns_of)
    if findings:
        raise CheckFailed(artefact.identity, findings)
