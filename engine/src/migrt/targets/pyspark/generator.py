"""Generation for the PySpark target (spec sections 5 and 6).

Deterministic: the same representation always gives the same file. A view and a table-valued function become a
function of session state returning a DataFrame, a scalar function without data access a function of session
state and its arguments returning a column expression. ANSI mode is pinned in every function. Every code line
is linked to the nodes it came from, and so to their source positions, and every expression is emitted as one
contiguous, self-contained span of code that layer 1 checks (`layer1.Span`).

A node kind is generated if the generator has a handler for it: a plan step, an expression, an artefact body, a
statement or a parameter, each declared by decorating the code that generates it (`PLAN_STEPS`, `EXPRESSIONS`,
`BODIES`, `STATEMENTS`, `PARAMETERS`), and `handled_kinds()` is what they are. Adding a handler is the only edit a kind
needs; detection refuses every other kind (issue #107).

Constructs the target cannot translate are detected first (`untranslatable.detect`), and generation must account
for each of them: an operation this target cannot yet generate exactly is declared unsupported here and becomes a
manual item. Its column is emitted as `F.raise_error(...)`, so the file still runs its other columns and fails
loudly, never silently, where the item is used; a plan step that cannot be generated makes the function raise
when called. A procedure, trigger, batch script, multi-statement table function, or a scalar function that is
not one expression, is procedural generation (issue #50). A procedure, a batch or a trigger whose every statement
is generated runs as Python through session state: variables are Python values (`None` is NULL), conditions and
arithmetic go through the twins (`ss.twin`), each statement leaves in `@@ROWCOUNT` and `@@ERROR` what the source's
statement effects say, and a procedure gives back one result object (`ss.result`). One with a statement that is not
generated yet has a manual item for each such statement and raises when called, so nothing of it runs; the file holds
the signature and the reasons. A scalar function that is more than one expression, and a multi-statement table
function, still hold one manual item for each statement. A finding that no manual item covers is `DroppedConstruct`,
an error: nothing is dropped. The report lists every manual item with its reason and source position.

Where a reason or caveat comes from a silent difference, this target names the entry and
its own reason only; the wording of the difference comes from the core's catalogue, so
the source's behaviour is worded by the source half (ADR 0019).
"""
from __future__ import annotations

import ast
import dataclasses
import functools
import keyword
import re
from collections.abc import Callable, Sequence
from typing import NoReturn, Protocol

from ...core import vocabulary
from ...core.differences import Catalogue
from ...core.ir import Artefact, Node, VType
from . import layer1, untranslatable
from .draft import Draft, GivenUp, Lines, ManualItem, Unsupported
from . import types as spark_types
from .names import Names

ANSI_PIN = 'spark.conf.set("spark.sql.ansi.enabled", "true")'
SESSION_SPARK = "spark = ss.spark"
FUNCTIONS_IMPORT = "from pyspark.sql import functions as F"
AGGREGATE_KINDS = {"count_values", "count_rows", "sum", "average", "minimum", "maximum"}
COMPARE = {"=": "==", "<>": "!=", ">": ">", "<": "<", ">=": ">=", "<=": "<="}
MONEY = "3.11"                # silent difference: money arithmetic
MONEY_NOT_BUILT = "it needs the support library's money type, which is not built yet"
SUM_INT = "3.14"              # silent difference: a running integer total that leaves int
STOPS_AT_ERROR = "7.2"        # silent difference: a failed statement ends the function where the source goes on (ADR 0004)
UDF_FALLBACK = "#64"          # raising a scalar function to one expression, and the Python UDF fallback
STRING_COMPARISON = "comparing strings by a named rule needs the support library's comparison helpers (not built yet)"
PROJECT_PACKAGE = "generated"
DESCRIPTIONS = {"view": "a view", "inline_function": "an inline table-valued function",
                "scalar_function": "a scalar function", "table_function": "a multi-statement table-valued function",
                "procedure": "a stored procedure", "trigger": "a trigger", "query_batch": "a query batch script"}
SPECIAL_CAUSES = {*untranslatable.SPECIAL, untranslatable.STALE, untranslatable.UNSUPPORTED_TYPE}
# What a statement may be without being able to fail: reading these, comparing and assigning values cannot raise an error.
CANNOT_FAIL = frozenset({"procedure_body", "trigger_body", "batch_script_body", "batch", "declare_parameter", "literal", "null",
                         "variable", "compare", "and", "or", "not", "is_null", "rows_affected", "last_statement_error",
                         "declare_variable", "assign", "if", "while", "break", "continue", "return"})
VALUE_TYPES = ("integer", "string")      # the types a variable's value is held as in procedural code


class Effect(Protocol):
    """What a statement leaves in `@@ROWCOUNT` and `@@ERROR`, as the source says (`rows`: keeps, rows, one, zero, fetch,
    callee or unknown; `error`: keeps, reset or unknown). The source half decides it; generation only carries it out."""
    @property
    def rows(self) -> str: ...

    @property
    def error(self) -> str: ...


class DroppedConstruct(Exception):
    """A construct detected before generation that no manual item covers: generation would have dropped it."""


@dataclasses.dataclass(frozen=True)
class Dependencies:
    """What the artefact reads, as the project sees it: the catalog kind of each thing it depends on, and which
    of them are generated into this project (a dependency there is an import)."""
    kinds: dict[str, str] = dataclasses.field(default_factory=dict)
    generated: set[str] = dataclasses.field(default_factory=set)


@dataclasses.dataclass
class GeneratedFile:
    relative_path: str
    text: str
    links: dict[int, list[str]]           # generated line number -> node ids
    manual_items: list[ManualItem]
    differences: dict[str, str]           # node id -> the silent difference it carries as a caveat
    escape_hatch_uses: list[str]          # node ids generated as spark.sql (none yet)
    name_adjustments: dict[str, dict[str, str]]   # rule -> {source name: the changed name this file uses}
    expressions: dict[str, layer1.Span]   # expression node id -> where its code sits
    expression_nodes: list[str]           # every expression node whose code is in the file
    findings: list[untranslatable.Finding]   # detected before generation
    settings_used: list[str]              # the named settings the code reads, for the settings file
    assumptions: list[str]                # what generation assumed, each worded for the report
    functions: list[str]                  # the functions the file defines, in order
    parameters: list[dict]                # the entry point's parameters, as the manifest lists them
    columns: list[dict] | None            # the columns of the DataFrame a view or table function returns
    result: str                           # dataframe or column: what the function returns when it runs
    signature: str                        # the arguments of the entry point, after the name
    fails_when_called: bool               # the function raises when called: all of it is manual work
    variables: list[str] = dataclasses.field(default_factory=list)   # the Python names of the source's variables
    readable: dict[str, list[str]] = dataclasses.field(default_factory=dict)   # expression node -> variables in its scope
    discarded: list[str] = dataclasses.field(default_factory=list)   # procedural functions whose code the file does not hold:
                                                                     # the ids of their roots, each with manual items under it


def spark_type(t: VType, node: Node) -> str:
    """The Spark type to cast a value to, where `cast` alone keeps the meaning."""
    found = spark_types.mapping(t)
    if found is None:
        raise Unsupported(node, f"no PySpark type mapping for {t} yet")
    if found.inexact_cast:
        raise Unsupported(node, found.inexact_cast, found.waits_for)
    return found.ddl


def prose(text: str) -> str:
    """Client text (an identity, a file's path) put in a docstring: it can never end the docstring or escape."""
    return text.replace("\\", "/").replace('"', "'")


def python_name(name: str, taken: set[str]) -> str:
    base = re.sub(r"\W", "_", name) or "_"
    if base[0].isdigit():
        base = "_" + base
    candidate, n = base, 2
    while keyword.iskeyword(candidate) or candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    taken.add(candidate)
    return candidate


class Handlers:
    """The generator's handlers for one role, by the node kind each one generates (issue #107). A handler is declared
    by decorating the code that generates the kind, and that is the only edit a kind needs: `handled_kinds()` is
    what the handlers are, and detection before generation refuses every other kind. A handler may still decline
    a particular node (a money product, a string compared by a collation) by raising `Unsupported` with its own
    reason."""

    def __init__(self, role: str):
        self.role = role
        self.by_kind: dict[str, Callable] = {}

    def __call__(self, *kinds: str):
        def register(handler):
            for kind in kinds:
                if kind in self.by_kind:
                    raise ValueError(f"{kind} has two {self.role} handlers")
                self.by_kind[kind] = handler
            return handler
        return register

    def of(self, node: Node) -> Callable:
        """The handler for a node of a kind that must have one (a malformed representation does not)."""
        found = self.by_kind.get(node.kind)
        if found is None:
            raise ValueError(f"{node.id}: no {self.role} handler for a {node.kind}")
        return found


PLAN_STEPS = Handlers("plan step")            # (generator, node) -> the variable holding the step
EXPRESSIONS = Handlers("expression")          # (generator, node, grouped) -> code
BODIES = Handlers("artefact body")            # (generator, function name, body); the file's functions
STATEMENTS = Handlers("statement")            # (generator, statement, body); a scalar function's only statement
PARAMETERS = Handlers("parameter")            # (generator, parameter node)
VALUES = Handlers("value expression")         # (generator, node) -> code of a Python value; the kind may be an expression too
ROLES = (PLAN_STEPS, EXPRESSIONS, BODIES, STATEMENTS, PARAMETERS)


def handled_kinds() -> frozenset[str]:
    """Every node kind the generator has a handler for, in any role. A kind has one handler in one role."""
    kinds = [kind for role in ROLES for kind in role.by_kind]
    if len(kinds) != len(set(kinds)):
        raise ValueError(f"a kind has handlers in two roles: {sorted(k for k in set(kinds) if kinds.count(k) > 1)}")
    return frozenset(kinds) | frozenset(VALUES.by_kind)


def detect(artefact: Artefact) -> list[untranslatable.Finding]:
    """Every node the target cannot translate, detected before generation against the kinds it has handlers for."""
    return untranslatable.detect(artefact, handled_kinds())


class Generator:
    def __init__(self, artefact: Artefact, snapshot_id: str, tool_version: str, differences: Catalogue,
                 names: Names, dependencies: Dependencies, effects: Callable[[Node], Effect] | None = None):
        self.artefact = artefact
        self.effects = effects
        self.recorded_names = names
        self.differences = differences
        self.dependencies = dependencies
        self.snapshot_id = snapshot_id
        self.tool_version = tool_version
        self.draft = Draft()
        self.handled = handled_kinds()
        self.findings = untranslatable.detect(artefact, self.handled)
        self.detected: dict[str, list[untranslatable.Finding]] = {}
        for found in self.findings:
            self.detected.setdefault(found.node, []).append(found)
        self.params: dict[str, str] = {}               # parameter name, casefolded -> its Python name
        self.parameters: list[dict] = []
        self.functions: list[str] = []
        self.columns: list[dict] | None = None
        self.result = "none"
        self.signature_text: str | None = None
        self.variables: list[str] = []                 # the Python names of the source's variables, in the file
        # the function being generated, when it is procedural
        self.depth = 1                                 # the indentation of the code being emitted, in levels
        self.collects = False                          # it gives back result sets (a procedure or a batch)
        self.result_sets = "result_sets"
        self.guards_needed = False                     # it reads @@ERROR and has a statement that could fail
        self.scopes: list[list[str]] = []              # per statement being generated: the variables in scope before it
        self.readable: dict[str, list[str]] = {}       # expression node id -> the source variables its code may read
        self.discarded: list[Node] = []                # the procedural functions whose code is not in the file

    # ------------------------------------------------------------------ the file

    def generate(self) -> GeneratedFile:
        a = self.artefact
        path, name = self.recorded_names.module(a.identity, a.artefact_type)
        self.draft.taken.add(name)
        handler = BODIES.by_kind.get(a.body.kind)
        if handler is None:
            raise ValueError(f"{a.identity}: a {a.artefact_type} is not generated")
        handler(self, name, a.body)
        require_accounted(self.findings, self.draft.manual, a)
        return self.assemble(path)

    def assemble(self, path: str) -> GeneratedFile:
        a = self.artefact
        uses = {n.id for n in ast.walk(ast.parse("\n".join(self.draft.lines))) if isinstance(n, ast.Name)}
        header = Lines()
        emit = header.emit
        emit(f'"""{prose(a.identity)}, {DESCRIPTIONS[a.artefact_type]}, generated as PySpark by migrt {self.tool_version}.')
        emit("")
        emit(f"Generated from catalog snapshot {self.snapshot_id}. Never edit this file by hand: the tool")
        emit('regenerates it. Comments marked "original:" are carried unchanged from the source.')
        emit('"""')
        if "Column" in uses:
            emit("from pyspark.sql import Column")
        if "DataFrame" in uses:
            emit("from pyspark.sql import DataFrame")
        if "F" in uses:
            emit(FUNCTIONS_IMPORT)
        for line in sorted(self.draft.imports.values()):
            emit(line)
        emit("")
        emit("")
        header.append(self.draft)
        nodes = {n.id: n for n in a.nodes()}
        replaced = {n.id for m in self.draft.manual for n in nodes[m.replaces].walk()}
        replaced |= {n.id for container in self.discarded for n in container.walk()}
        expression_nodes = [n.id for n in a.nodes() if vocabulary.part_of(n.kind) == "expression"
                            and n.id not in replaced]
        used = {rule: dict(sorted(names.items())) for rule, names in sorted(self.recorded_names.used.items())}
        return GeneratedFile(path, "\n".join(header.lines) + "\n", header.links, self.draft.manual, self.draft.caveats, [],
                             used, header.spans, expression_nodes, self.findings, sorted(set(self.draft.settings_used)),
                             self.draft.assumptions, self.functions, self.parameters, self.columns, self.result,
                             self.signature_text or "ss", self.draft.fails_when_called, variables=self.variables,
                             readable=self.readable, discarded=[c.id for c in self.discarded])

    def declare_parameters(self, body: Node) -> list[Node]:
        """The parameter nodes, each with its Python name; a default is the caller's to pass (reported)."""
        declared = body.children_with("parameter")
        for p in declared:
            PARAMETERS.of(p)(self, p)
        return declared

    @PARAMETERS("declare_parameter")
    def declare_parameter(self, p: Node) -> None:
        name = p.attrs["name"]
        python = python_name(name.lstrip("@"), self.draft.taken)
        self.params[name.casefold()] = python
        spark = spark_types.mapping(p.type) if p.type is not None else None
        if spark is not None and spark.companion_column:
            self.requirement(f"parameter {name}", spark)
        self.parameters.append({"name": name, "python_name": python, "direction": p.attrs["direction"],
                                "readonly": p.attrs["readonly"], "default": p.attrs["default"],
                                "type": p.type.to_json() if p.type is not None else None,
                                "spark_type": spark.ddl if spark else None})

    def requirement(self, what: str, found: spark_types.SparkType) -> None:
        """A requirement on the target tables that the type's representation makes, listed in the report."""
        self.draft.assumptions.append(f"{self.artefact.identity}: {what} has a type held with a companion "
                                f"{found.companion_column} column in the target table, which that table must have")

    def signature(self, declared: Sequence[Node], *, positional: bool, extra: Sequence[str] = ()) -> str:
        """The arguments of the function. A procedure's are keyword-only, and one with a default can be left out (`...` is
        what the caller passes for DEFAULT, and what the function sees when it is left out). A function's are positional,
        since T-SQL does not let a call leave one out: it passes DEFAULT, and the function applies the default."""
        names = [self.params[p.attrs["name"].casefold()] + ("" if positional or not p.children_with("default") else "=...")
                 for p in declared] + list(extra)
        self.draft.taken.update(extra)
        return ", ".join(["ss"] + ([] if positional or not names else ["*"]) + names)

    def apply_defaults(self, declared: Sequence[Node], owner: Node) -> GivenUp | None:
        """For each parameter with a default: if the argument was left out or is DEFAULT (`...`), it takes the default, which
        is the value the source converts it to, as T-SQL does. Where one cannot be generated the function is manual work (its
        item replaces the whole function), and the first such item is returned."""
        failed: GivenUp | None = None
        for p in declared:
            default = p.children_with("default")
            if not default:
                continue
            python = self.params[p.attrs["name"].casefold()]
            code = self.draft.attempt(lambda: self.value(default[0]), replaces=owner)
            if isinstance(code, GivenUp):
                failed = failed or code
                continue
            self.put(f"if {python} is ...:", [_shallow(p)])
            self.put(f"    {python} = {code}", [p])
        return failed

    @property
    def pad(self) -> str:
        return "    " * self.depth

    def put(self, code: str, nodes: Sequence[Node] = ()) -> None:
        """One line of code at the indentation of the block being generated."""
        self.draft.emit(self.pad + code, nodes)

    def open_function(self, name: str, signature: str, annotation: str, docstring: str, declared: Sequence[Node],
                      *, reads_tables: bool, owner: Node | None = None) -> None:
        """The def line holds the function's own node, where it has one, and its parameters."""
        emit = self.draft.emit
        self.functions.append(name)
        self.signature_text = self.signature_text or signature
        emit(f"def {name}({signature}) -> {annotation}:", ([_shallow(owner)] if owner else []) + list(declared))
        emit(f'    """{docstring} `ss` is the session state."""')
        if reads_tables:
            emit(f"    {SESSION_SPARK}")
            emit(f"    {ANSI_PIN}")
        else:
            emit(f"    ss.{ANSI_PIN}")

    # ------------------------------------------------------------------ views and table functions

    @BODIES("view_body")
    def view_body(self, name: str, body: Node) -> None:
        self.function_returning_dataframe(name, body, "view")

    @BODIES("table_function_body")
    def table_function_body(self, name: str, body: Node) -> None:
        """Inline and multi-statement table functions share a body kind: the artefact tells them apart."""
        if self.artefact.artefact_type == "inline_function":
            self.function_returning_dataframe(name, body, "table function")
        else:
            self.stub(name, body, "DataFrame", "multi-statement table function", positional=True)

    @BODIES("procedure_body")
    def procedure_body(self, name: str, body: Node) -> None:
        self.procedural(name, body, "procedure", "object", "procedure", positional=False)

    @BODIES("trigger_body")
    def trigger_body(self, name: str, body: Node) -> None:
        self.procedural(name, body, "trigger", "None", "trigger", positional=True, extra=["inserted", "deleted"])

    def function_returning_dataframe(self, name: str, body: Node, noun: str) -> None:
        a, emit = self.artefact, self.draft.emit
        declared = self.declare_parameters(body)
        self.open_function(name, self.signature(declared, positional=True), "DataFrame",
                           f"The {noun} {prose(a.identity)}.", declared, reads_tables=True, owner=body)
        self.draft.comments(body, "    ")
        query = body.child("query")
        self.result, self.columns = "dataframe", self.result_columns(query)
        defaults = self.apply_defaults(declared, body)
        if defaults:
            self.fail_function(defaults, body, body)
            return
        result = self.draft.attempt(lambda: self.plan(query), replaces=query)
        if isinstance(result, GivenUp):
            self.fail_function(result, query, body)
        else:
            emit(f"    return {result}", [body])

    def fail_function(self, given_up: GivenUp, source: Node, body: Node) -> None:
        """What stands in the file for a function body given up: it raises on the call, not on evaluation, since an
        empty result would hide the failure."""
        self.draft.emit(f"    # source {source.span}")
        self.draft.fail_when_called(given_up.item.reason, [body])

    def result_columns(self, query: Node) -> list[dict] | None:
        """The columns the query returns, from its select list; None where the query is not a select, which the
        report says."""
        node = query
        while node.kind not in ("project", "aggregate"):
            inputs = node.children_with("input") or node.children_with("left")
            if not inputs:
                self.draft.assumptions.append(f"{self.artefact.identity}: its result is not a plain select list, so the "
                                        "manifest does not list its columns")
                return None
            node = inputs[0]
        out = []
        for output in node.children_with("output"):
            found = spark_types.mapping(output.type) if output.type is not None else None
            if found is None:
                self.draft.assumptions.append(f"{self.artefact.identity}: the column {output.attrs.get('output_name')} has "
                                        f"the type {output.type}, which has no Spark type: the manifest lists it "
                                        "without one")
            if found is not None and found.companion_column:
                self.requirement(f"column {output.attrs.get('output_name')}", found)
            out.append({"name": output.attrs.get("output_name"),
                        "type": output.type.to_json() if output.type is not None else None,
                        "spark_type": found.ddl if found else None})
        return out

    # ------------------------------------------------------------------ scalar functions

    @BODIES("scalar_function_body")
    def scalar_function(self, name: str, body: Node) -> None:
        a = self.artefact
        statements = body.children_with("statement")
        simple = len(statements) == 1 and statements[0].kind == "return" and statements[0].children_with("argument")
        if not simple:
            self.stub(name, body, "Column", "scalar function", positional=True)
            return
        declared = self.declare_parameters(body)
        self.open_function(name, self.signature(declared, positional=True), "Column",
                           f"The scalar function {prose(a.identity)}, a column expression.", declared, reads_tables=False,
                           owner=body)
        self.draft.comments(body, "    ")
        self.result = "column"
        defaults = self.apply_defaults(declared, body)
        if defaults:
            self.fail_function(defaults, body, body)
            return
        STATEMENTS.of(statements[0])(self, statements[0], body)

    @STATEMENTS("return")
    def return_statement(self, ret: Node, body: Node) -> None:
        if body.kind != "scalar_function_body":
            self.return_from_routine(ret, body)
            return
        code = self.draft.attempt(lambda: self.expr(ret.child("argument"), {}), replaces=ret)
        if isinstance(code, GivenUp):
            self.fail_function(code, ret, body)
        else:
            self.draft.emit(f"    # source {ret.span}")
            self.draft.emit(f"    return {code}", [ret])

    # ------------------------------------------------------------------ procedural artefacts (issue #50)

    def procedural(self, name: str, container: Node, role: str, annotation: str, noun: str, *, positional: bool,
                   extra: Sequence[str] = (), docstring: str | None = None, link: Sequence[Node] = ()) -> None:
        """A procedure, a batch or a trigger: every statement generated, or the function raises when called and each
        statement not generated is a manual item. `role` is `procedure`, `batch` or `trigger`.

        Nothing of a function that has a manual item runs, and its file holds none of its code: the statements are
        generated one by one so that each one that cannot be is reported with its own reason, and if any was not, the
        code is taken out again and the signature and the reasons stay."""
        declared = self.declare_parameters(container)
        self.open_function(name, self.signature(declared, positional=positional, extra=extra), annotation,
                           docstring or f"The {noun} {prose(self.artefact.identity)}.", declared, reads_tables=True,
                           owner=container)
        for node in link:
            line = len(self.draft.lines)
            self.draft.links[line] = sorted({*self.draft.links.get(line, []), node.id}, key=lambda i: int(i[1:]))
        self.draft.comments(container, "    ")
        saved = self.draft.checkpoint()
        before = len(self.draft.manual)
        self.depth, self.collects = 1, role != "trigger"
        self.result_sets = python_name("result_sets", self.draft.taken)
        self.guards_needed = (any(n.kind == "last_statement_error" for n in container.walk())
                              and any(n.kind not in CANNOT_FAIL for n in container.walk()))
        self.result = "procedure" if self.collects else "none"
        self.account_for_parameters(declared)
        self.apply_defaults(declared, container)
        self.hoist_variables(container)
        if self.collects:
            self.put(f"{self.result_sets} = []", [_shallow(container)])
        if role == "procedure":
            short = self.artefact.identity.rsplit(".", 1)[-1]
            self.put(f"with ss.scope({short!r}, procedure_call=True):", [_shallow(container)])
            self.depth += 1
        statements = container.children_with("statement")
        for statement in statements:
            self.statement(statement, container)
        if self.collects and not (statements and statements[-1].kind == "return"):
            self.put(f"return ss.result(0, {self.outputs(declared)}, {self.result_sets})", [_shallow(container)])
        self.depth = 1
        added = self.draft.manual[before:]
        if added:
            self.discard(saved, added, name, noun, container)

    def hoist_variables(self, container: Node) -> None:
        """A variable is declared for the whole batch, and is NULL until something assigns it: a declaration run again in a
        loop does not reset it, and one in a branch not taken leaves it NULL for the code after. So every variable of the
        function starts as `None` at the top, and a declaration only assigns its initial value."""
        for node in container.walk():
            if node.kind != "declare_variable":
                continue
            name = node.attrs["name"]
            python = python_name(name.lstrip("@"), self.draft.taken)
            self.params[name.casefold()] = python
            self.variables.append(python)
            self.put(f"{python} = None", [_shallow(node)])

    def account_for_parameters(self, declared: Sequence[Node]) -> None:
        """A parameter of an unsupported type, or one on a scope that may be stale, is manual work in its own right."""
        for p in declared:
            causes = [f for f in self.detected.get(p.id, []) if f.kind in SPECIAL_CAUSES]
            if causes:
                self.draft.manual_item_at(p, "; ".join(f.reason for f in causes),
                                          sorted({w for f in causes for w in f.waits_for}))

    def discard(self, saved: dict, added: list[ManualItem], name: str, noun: str, container: Node) -> None:
        """The function has manual work, so none of its code is in the file: it is as it was before the first statement,
        and says which statements are manual and why, and raises when called."""
        self.draft.restore(saved)
        self.discarded.append(container)
        self.draft.manual += added
        reasons: dict[str, list[ManualItem]] = {}
        for item in added:
            reasons.setdefault(item.replaces, []).append(item)
        for node in container.walk():                       # the comments of every statement stay, the reasons beside theirs
            if node is not container:
                self.draft.comments(node, "    ")
            for item in reasons.get(node.id, []):
                self.draft.emit(f"    # {item.reason}")
        self.draft.fail_when_called(self.stub_message(name, noun, len(added)), [container])

    def outputs(self, declared: Sequence[Node]) -> str:
        """The output parameters as the result object holds them: by Python name, with the value each ended with."""
        names = [self.params[p.attrs["name"].casefold()] for p in declared if p.attrs["direction"] == "output"]
        return "{" + ", ".join(f"{n!r}: {n}" for n in names) + "}"

    def statement(self, node: Node, container: Node) -> None:
        """One statement of the function: generated, or given up as a manual item with its code left out."""
        self.draft.attempt(lambda: self.generate_statement(node, container), replaces=node)

    def generate_statement(self, node: Node, container: Node) -> None:
        self.refuse_detected(node)
        handler = STATEMENTS.by_kind.get(node.kind)
        if handler is None:
            raise Unsupported(node, untranslatable.procedural_reason(node.kind), (untranslatable.PROCEDURAL,))
        self.draft.comments(node, self.pad)
        self.scopes.append(node.attrs.get("scope", {}).get("variables", []))
        try:
            handler(self, node, container)
        finally:
            self.scopes.pop()

    def block(self, statements: list[Node], container: Node, owner: Node) -> None:
        """The statements of a branch or a loop body, one level in."""
        self.depth += 1
        try:
            mark = len(self.draft.lines)
            for statement in statements:
                self.statement(statement, container)
            if len(self.draft.lines) == mark:
                self.put("pass", [_shallow(owner)])
        finally:
            self.depth -= 1

    def done(self, node: Node, rows: str | None = None) -> None:
        """What the statement leaves in `@@ROWCOUNT` and `@@ERROR` once it has succeeded, as the source says it does. `rows`
        is the code of the count, for a statement that leaves the number of rows it read or changed."""
        if self.effects is None:
            raise Unsupported(node, "what each statement leaves in @@ROWCOUNT and @@ERROR was not given to generation",
                              (untranslatable.PROCEDURAL,))
        effect = self.effects(node)
        counts = {"keeps": None, "one": "1", "zero": "0", "rows": rows}.get(effect.rows, ...)
        if counts is ... or (effect.rows == "rows" and rows is None) or effect.error not in ("keeps", "reset"):
            raise Unsupported(node, f"what a {node.kind} leaves in @@ROWCOUNT and @@ERROR is not known to generation",
                              (untranslatable.PROCEDURAL,))
        if counts is None and effect.error == "keeps":
            return
        self.put(f"ss.statement_done({counts}{'' if effect.error == 'reset' else ', reset_error=False'})",
                 [_shallow(node)])

    # ---- the statements

    @STATEMENTS("declare_variable")
    def declare_variable(self, s: Node, container: Node) -> None:
        initial = s.children_with("initial")
        if initial:
            self.put(f"{self.params[s.attrs['name'].casefold()]} = {self.value(initial[0])}", [s])
        self.done(s)

    @STATEMENTS("assign")
    def assign(self, s: Node, container: Node) -> None:
        python = self.params.get(s.attrs["variable"].casefold())
        if python is None:
            raise Unsupported(s, f"{s.attrs['variable']} is not a parameter or a variable declared before this point")
        self.put(f"{python} = {self.value(s.child('argument'))}", [s])
        self.done(s)

    @STATEMENTS("if")
    def if_statement(self, s: Node, container: Node) -> None:
        condition = s.child("condition")
        flag = python_name("condition", self.draft.taken)
        self.put(f"{flag} = ss.twin.holds({self.value(condition)})", [_shallow(s), condition])
        self.done(s)
        self.put(f"if {flag}:", [_shallow(s)])
        self.block(s.children_with("then"), container, s)
        if s.children_with("else"):
            self.put("else:", [_shallow(s)])
            self.block(s.children_with("else"), container, s)

    @STATEMENTS("while")
    def while_statement(self, s: Node, container: Node) -> None:
        condition = s.child("condition")
        flag = python_name("condition", self.draft.taken)
        self.put("while True:", [_shallow(s)])
        self.depth += 1
        try:
            self.put(f"{flag} = ss.twin.holds({self.value(condition)})", [_shallow(s), condition])
            self.done(s)
            self.put(f"if not {flag}:", [_shallow(s)])
            self.put("    break", [_shallow(s)])
        finally:
            self.depth -= 1
        self.block(s.children_with("body"), container, s)

    @STATEMENTS("break")
    def break_statement(self, s: Node, container: Node) -> None:
        self.put("break", [s])
        self.done(s)

    @STATEMENTS("continue")
    def continue_statement(self, s: Node, container: Node) -> None:
        self.put("continue", [s])

    def return_from_routine(self, ret: Node, container: Node) -> None:
        argument = ret.children_with("argument")
        if not self.collects:
            if argument:
                raise Unsupported(ret, "a trigger returns no value")
            self.done(ret)
            self.put("return", [ret])
            return
        code = "0"
        if argument:
            code = python_name("return_code", self.draft.taken)
            self.put(f"{code} = {self.value(argument[0])}", [ret])
        self.done(ret)
        self.put(f"return ss.result({code}, {self.outputs(container.children_with('parameter'))}, {self.result_sets})",
                 [ret])

    @STATEMENTS("emit_result_set")
    def emit_result_set(self, s: Node, container: Node) -> None:
        if not self.collects:
            raise Unsupported(s, "a trigger that returns a result set is not generated: the result would go nowhere")
        mark = len(self.draft.lines)
        frame = self.plan(s.child("query"))
        self.draft.indent_since(mark, "    " * (self.depth - 1))
        self.draft.caveats[s.id] = STOPS_AT_ERROR
        self.put(f"{self.result_sets}.append({frame})", [s])
        self.done(s, f"{frame}.count()")

    # ------------------------------------------------------------------ values of procedural code

    def value(self, node: Node) -> str:
        """The code of one expression as a Python value (`None` is NULL), recorded so that layer 1 can check it alone."""
        self.refuse_detected(node)
        handler = VALUES.by_kind.get(node.kind)
        if handler is None:
            raise Unsupported(node, f"{node.kind} is not generated in procedural code yet", (untranslatable.PROCEDURAL,))
        code = handler(self, node)
        self.draft.pending.append((node.id, code))
        self.note_readable(node)
        return code

    def note_readable(self, node: Node) -> None:
        """What the code of this expression may read: the source variables in scope before its statement, as the
        representation says (a parameter is an argument of the function, and always readable)."""
        in_scope = self.scopes[-1] if self.scopes else []
        self.readable[node.id] = sorted({self.params[name.casefold()] for name in in_scope if name.casefold() in self.params})

    def values(self, node: Node) -> list[str]:
        """The code of an expression's arguments, the first generated first."""
        return [self.value(x) for x in self.arguments(node)]

    @staticmethod
    def type_waits(t: VType | None) -> tuple[str, ...]:
        """What a type with no Python value in procedural code waits for: the number, date and time helpers for a number or
        a moment, and the rest of this work for any other."""
        if t is not None and t.name in ("decimal", "money", "float", "date", "local_timestamp", "timestamp_with_offset",
                                        "time_of_day"):
            return (untranslatable.NUMBER_HELPERS,)
        return (untranslatable.PROCEDURAL,)

    def integer_type(self, node: Node) -> tuple[int, bool]:
        """The width and sign of an integer-typed node, or the refusal to generate anything else in procedural code."""
        t = node.value_type
        if t.name != "integer":
            raise Unsupported(node, f"{node.kind} on {node.type} is not generated in procedural code yet",
                              self.type_waits(t))
        return t.int_param("bits"), t.flag("signed")

    @VALUES("literal")
    def value_literal(self, node: Node) -> str:
        if "secret_setting" in node.attrs:
            settings = node.attrs.get("secret_settings") or [node.attrs["secret_setting"]]
            if len(settings) > 1:
                raise Unsupported(node, f"the literal holds {len(settings)} marked secrets ({', '.join(settings)}): one "
                                        "setting cannot stand for all of them")
            if node.value_type.name != "string":
                raise Unsupported(node, f"a marked secret of type {node.type} is not generated in procedural code yet",
                                  self.type_waits(node.value_type))
            self.draft.settings_used.append(settings[0])
            return f'ss.setting("{settings[0]}")'
        if node.value_type.name not in VALUE_TYPES:
            raise Unsupported(node, f"a literal of type {node.type} is not generated in procedural code yet",
                              self.type_waits(node.value_type))
        return repr(node.attrs["value"])

    @VALUES("null")
    def value_null(self, node: Node) -> str:
        return "None"

    @VALUES("variable")
    def value_variable(self, node: Node) -> str:
        python = self.params.get(str(node.attrs["name"]).casefold())
        if python is None:
            raise Unsupported(node, f"{node.attrs['name']} is not a parameter or a variable declared before this point")
        return python

    @VALUES("compare")
    def value_compare(self, node: Node) -> str:
        if node.attrs.get("rule"):
            raise Unsupported(node, STRING_COMPARISON, (untranslatable.STRING_HELPERS,))
        a, b = self.values(node)
        return f'ss.twin.compare("{node.attrs["operator"]}", {a}, {b})'

    @VALUES("and", "or")
    def value_and_or(self, node: Node) -> str:
        a, b = self.values(node)
        return f"ss.twin.{node.kind}_({a}, {b})"

    @VALUES("not")
    def value_not(self, node: Node) -> str:
        return f"ss.twin.not_({self.values(node)[0]})"

    @VALUES("is_null")
    def value_is_null(self, node: Node) -> str:
        return f"ss.twin.is_null({self.values(node)[0]})"

    @VALUES("add", "subtract", "multiply", "modulo", "integer_divide")
    def value_arithmetic(self, node: Node) -> str:
        if node.type is not None and node.type.name == "money":
            self.refuse_money(node)
        bits, signed = self.integer_type(node)
        a, b = self.values(node)
        self.draft.caveats[node.id] = STOPS_AT_ERROR
        return f"ss.twin.{node.kind}({a}, {b}, bits={bits}, signed={signed})"

    @VALUES("implicit_convert")
    def value_implicit_convert(self, node: Node) -> str:
        [argument] = self.arguments(node)
        if argument.value_type.name == "integer" and node.value_type.name == "integer":
            bits, signed = self.integer_type(node)
            self.draft.caveats[node.id] = STOPS_AT_ERROR
            return f"ss.twin.to_integer({self.value(argument)}, bits={bits}, signed={signed})"
        raise Unsupported(node, f"the conversion of {argument.type} to {node.type} is not generated in procedural code yet",
                          self.type_waits(node.value_type if argument.value_type.name == "string" else argument.value_type))

    @VALUES("rows_affected")
    def value_rows_affected(self, node: Node) -> str:
        return "ss.rowcount"

    @VALUES("last_statement_error")
    def value_last_statement_error(self, node: Node) -> str:
        if self.guards_needed:
            raise Unsupported(node, "a procedure that reads @@ERROR and has a statement that could fail needs each statement "
                                    "in a guarded block, so that the error number is there to read and the procedure goes "
                                    "on: guarded blocks are not generated yet", (untranslatable.PROCEDURAL,))
        return "ss.at_error"

    @BODIES("batch_script_body")
    def batches(self, module_name: str, body: Node) -> None:
        names = []
        for number, batch in enumerate(body.children_with("batch"), start=1):
            name = python_name(f"batch_{number}", self.draft.taken)
            names.append(name)
            if number > 1:
                self.draft.emit("")
                self.draft.emit("")
            self.params = {}                          # a variable is declared for its own batch only
            BODIES.of(batch)(self, name, batch)
        self.draft.emit("")
        self.draft.emit("")
        self.draft.emit(f"BATCHES = [{', '.join(names)}]")

    @BODIES("batch")
    def batch(self, name: str, batch: Node) -> None:
        """One `GO`-separated batch of a script: a function of session state that gives back its result sets."""
        parent = self.artefact.body
        number = parent.children_with("batch").index(batch) + 1
        self.procedural(name, batch, "batch", "object", f"batch {number}", positional=True,
                        docstring=f"Batch {number} of the script {prose(self.artefact.identity)}.",
                        link=[parent] if number == 1 else [])

    # ------------------------------------------------------------------ functions that are not one expression: reported, not generated

    def stub(self, name: str, body: Node, annotation: str, noun: str, *, positional: bool,
             extra: Sequence[str] = ()) -> None:
        declared = self.declare_parameters(body)
        self.open_function(name, self.signature(declared, positional=positional, extra=extra), annotation,
                           f"The {noun} {prose(self.artefact.identity)}.", declared, reads_tables=False, owner=body)
        self.report_block(body, body.children_with("statement"), name, noun, [body])

    def report_block(self, container: Node, statements: list[Node], name: str, noun: str, link: list[Node],
                     roots: Sequence[Node] = ()) -> None:
        """One manual item for each statement (and each batch), and for each node below one that is kept as
        written, dynamic, a schema or permission change, of an unsupported type, or on a scope that may be stale."""
        wanted = {n.id for n in statements} | {n.id for n in roots}
        items = []
        for node in container.walk():
            causes = self.detected.get(node.id, [])
            special = [f for f in causes if f.kind in SPECIAL_CAUSES]
            if node.id not in wanted and not special:
                continue
            if causes:
                reasons, waits_for = [f.reason for f in causes], sorted({w for f in causes for w in f.waits_for})
            else:
                reason, waits_for = self.statement_reason(container, node)
                reasons = [reason]
            items.append((node, self.draft.manual_item_at(node, "; ".join(reasons), waits_for)))
        for node, item in items:
            self.draft.comments(node, "    ")
            self.draft.emit(f"    # {item.reason}")
        self.draft.fail_when_called(self.stub_message(name, noun, len(items)), link)

    @staticmethod
    def statement_reason(container: Node, node: Node) -> tuple[str, tuple[str, ...]]:
        """Why a statement no finding covers is manual work, and what it waits for."""
        if container.kind == "scalar_function_body":
            return ("this function is more than one expression, so it is not a column expression; raising it to "
                    "one and the Python UDF fallback are not built yet", (UDF_FALLBACK,))
        return untranslatable.procedural_reason(node.kind), (untranslatable.PROCEDURAL,)

    @staticmethod
    def stub_message(name: str, noun: str, count: int) -> str:
        return (f"{name}: {count} parts of this {noun} are manual work (see the Manual work comments in the file); "
                "it cannot run until they are done")

    # ------------------------------------------------------------------ manual work

    def failing_column(self, item: ManualItem, output: Node) -> str:
        """A column that fails loudly on any row, typed as the output where the type allows, so its shape holds. A
        type with no Spark type leaves the column without a cast, and the report says so: the DataFrame's column
        then has the type of `raise_error`, not the output's. (A cast that Spark's `cast` alone would not keep
        exactly leaves the column bare too; the column fails on every row, and this is not recorded.)"""
        code = f"F.raise_error(F.lit({item.reason!r}))"
        found = spark_types.mapping(output.value_type)
        if found is None:
            self.draft.assumptions.append(
                f"{self.artefact.identity}: the column {output.attrs['output_name']} is generated without a cast: its "
                f"type {output.type} has no Spark type")
            return code
        return code if found.inexact_cast else f'{code}.cast("{found.ddl}")'

    # ------------------------------------------------------------------ plan steps

    def plan(self, node: Node) -> str:
        """Emit the statements for one plan step and return the variable holding it."""
        self.refuse_detected(node)
        for definition in node.children_with("definition"):
            raise Unsupported(definition, untranslatable.reason_for_kind(definition.kind, self.handled) or "not generated",
                              untranslatable.waits_for_kind(definition.kind, self.handled))
        handler = PLAN_STEPS.by_kind.get(node.kind)
        if handler is None:
            raise self.no_handler(node)
        return handler(self, node)

    def no_handler(self, node: Node) -> Unsupported:
        """Refusing a node of a kind this role has no handler for: detection's reason for the kind, or the plain one."""
        return Unsupported(node, untranslatable.reason_for_kind(node.kind, self.handled)
                           or f"{node.kind} has no PySpark generation yet",
                           untranslatable.waits_for_kind(node.kind, self.handled))

    @PLAN_STEPS("join")
    def join(self, node: Node) -> str:
        if node.attrs["kind"] == "cross" or not node.children_with("condition"):
            raise Unsupported(node, "a cross join has no PySpark generation yet")
        left, right = self.plan(node.child("left")), self.plan(node.child("right"))
        condition = self.expr(node.child("condition"), {})
        var = python_name("joined", self.draft.taken)
        self.step(node, f'{var} = {left}.join({right}, {condition}, "{node.attrs["kind"]}")')
        return var

    @PLAN_STEPS("filter")
    def filter_step(self, node: Node) -> str:
        source = self.plan(node.child("input"))
        var = python_name("filtered", self.draft.taken)
        self.step(node, f"{var} = {source}.filter({self.expr(node.child('condition'), {})})")
        return var

    def refuse_detected(self, node: Node) -> None:
        """A node detected before generation as untranslatable is never generated, whatever else is known of it."""
        found = self.detected.get(node.id)
        if found:
            raise Unsupported(node, "; ".join(f.reason for f in found),
                              tuple(sorted({w for f in found for w in f.waits_for})))

    @PLAN_STEPS("read_table")
    def read_table(self, node: Node) -> str:
        if node.attrs.get("named_query"):
            raise Unsupported(node, "reading a named query has no PySpark generation yet")
        if node.attrs.get("temp_table") or node.attrs.get("row_set"):
            raise Unsupported(node, "reading a temp table or a table variable is procedural code, which is not "
                                    "generated yet", (untranslatable.PROCEDURAL,))
        table, alias = node.attrs["table"], node.attrs["alias"]
        var = python_name(alias, self.draft.taken)
        kind = self.dependencies.kinds.get(table, "table")
        if kind == "table":
            code = f'spark.read.table("{self.recorded_names.table(table)}").alias("{alias}")'
        elif kind == "view" and table in self.dependencies.generated:
            code = f'{self.callee(table, kind)}(ss).alias("{alias}")'
        elif kind == "view":
            raise Unsupported(node, f"{table} is a view that is not generated in this project, so there is "
                                    "nothing to import or read: a view is not a table in the target")
        else:
            raise Unsupported(node, f"reading {table}, a {kind}, has no PySpark generation yet")
        self.step(node, f"{var} = {code}")
        return var

    def callee(self, identity: str, kind: str) -> str:
        """The Python name of another generated artefact, imported as the module its file is (a dependency is an
        import), under a different name if its own is taken in this file."""
        path, function = self.recorded_names.module(identity, kind)
        module = path.removesuffix(".py").replace("/", ".")
        for local, line in self.draft.imports.items():
            if line.startswith(f"from {module} import "):
                return local
        local = python_name(function, self.draft.taken) if function in self.draft.taken else function
        self.draft.taken.add(local)
        self.draft.imports[local] = (f"from {module} import {function}" if local == function
                               else f"from {module} import {function} as {local}")
        return local

    def step(self, node: Node, code: str) -> None:
        self.draft.comments(node, "    ")
        self.draft.emit(f"    # source {node.span}")
        self.draft.emit(f"    {code}", [node] if node.kind == "read_table" else [_shallow(node)])

    @PLAN_STEPS("project")
    def project(self, node: Node) -> str:
        source = self.plan(node.child("input"))
        var = python_name("projected", self.draft.taken)
        self.draft.comments(node, "    ")
        self.draft.emit(f"    # source {node.span}")
        self.draft.emit(f"    {var} = {source}.select(", [_shallow(node)])
        for output in node.children_with("output"):
            self.output_line(output, lambda o: self.expr(o, {}))
        self.draft.emit("    )", [_shallow(node)])
        return var

    @PLAN_STEPS("aggregate")
    def aggregate(self, node: Node) -> str:
        if node.children_with("having"):
            raise Unsupported(node, "HAVING has no PySpark generation yet")
        source = self.plan(node.child("input"))
        keys = node.children_with("key")
        outputs = node.children_with("output")
        key_alias: dict[tuple[str, str], str] = {}
        used: set[str] = {o.attrs["output_name"] for o in outputs}
        for key in keys:
            if key.kind != "column":
                raise Unsupported(key, "grouping by an expression is not generated yet")
            if key.attrs.get("rule"):
                raise Unsupported(key, f"grouping by strings: {STRING_COMPARISON}", (untranslatable.STRING_HELPERS,))
            ref = (key.attrs["relation"], key.attrs["column"])
            exact = [o.attrs["output_name"] for o in outputs if o.kind == "column"
                     and (o.attrs["relation"], o.attrs["column"]) == ref]
            key_alias[ref] = exact[0] if exact else python_name(f"{ref[0]}_{ref[1]}", used)
        var = python_name("grouped", self.draft.taken)
        self.draft.comments(node, "    ")
        self.draft.emit(f"    # source {node.span}")
        self.draft.emit(f"    {var} = {source}.groupBy(", [_shallow(node)])
        for key in keys:
            ref = (key.attrs["relation"], key.attrs["column"])
            code = f'F.col("{ref[0]}.{ref[1]}")'
            self.draft.pending.append((key.id, code))
            self.draft.emit(f'        {code}.alias("{key_alias[ref]}"),', [key])
        self.draft.emit("    ).agg(", [_shallow(node)])
        in_agg = [o for o in outputs if any(n.kind in AGGREGATE_KINDS for n in o.walk())]
        manual_outputs = set()
        for output in in_agg:
            code = self.draft.attempt(functools.partial(self.expr, output, {}), output=output)
            if isinstance(code, GivenUp):
                manual_outputs.add(output.id)
                continue
            self.draft.comments(output, "        ")
            self.draft.emit(f'        {code}.alias("{output.attrs["output_name"]}"),', [output])
        if len(manual_outputs) == len(in_agg):
            # Spark's agg needs one expression; a count keeps the groups when every one is manual.
            self.draft.emit('        F.count(F.lit(1)).alias("__groups"),', [_shallow(node)])
        self.draft.emit("    )", [_shallow(node)])
        selected = python_name("selected", self.draft.taken)
        self.draft.emit(f"    {selected} = {var}.select(", [_shallow(node)])
        manual_by_node = {m.node: m for m in self.draft.manual}
        for output in outputs:
            name = output.attrs["output_name"]
            if output.id in manual_by_node:
                self.draft.comments(output, "        ")
                self.draft.emit(f'        {self.failing_column(manual_by_node[output.id], output)}.alias("{name}"),', [output])
            elif output.id in {o.id for o in in_agg}:
                self.draft.emit(f'        F.col("{name}"),', [output])
            else:
                self.output_line(output, lambda o: self.expr(o, key_alias))
        self.draft.emit("    )", [_shallow(node)])
        return selected

    def output_line(self, output: Node, generate) -> None:
        name = output.attrs["output_name"]
        code = self.draft.attempt(functools.partial(generate, output), output=output)
        if isinstance(code, GivenUp):
            code = self.failing_column(code.item, output)
        self.draft.comments(output, "        ")
        self.draft.emit(f'        {code}.alias("{name}"),', [output])

    # ------------------------------------------------------------------ expressions

    def expr(self, node: Node, grouped: dict) -> str:
        """Code for one expression, recorded so that layer 1 can check it alone. `grouped` maps (relation,
        column) of grouping keys to their names after grouping; it is empty before grouping."""
        self.refuse_detected(node)
        code = self.expression(node, grouped)
        self.draft.pending.append((node.id, code))
        self.note_readable(node)
        return code

    def expression(self, node: Node, grouped: dict) -> str:
        handler = EXPRESSIONS.by_kind.get(node.kind)
        if handler is None:
            raise self.no_handler(node)
        return handler(self, node, grouped)

    @staticmethod
    def arguments(node: Node) -> list[Node]:
        return node.children_with("argument")

    def operands(self, node: Node, grouped: dict) -> tuple[str, str]:
        """The code of a binary expression's two arguments, the left generated first."""
        a, b = (self.expr(x, grouped) for x in self.arguments(node))
        return a, b

    @EXPRESSIONS("column")
    def expr_column(self, node: Node, grouped: dict) -> str:
        ref = (node.attrs["relation"], node.attrs["column"])
        if grouped:
            if ref not in grouped:
                raise Unsupported(node, "a column outside an aggregate that is not a grouping key")
            return f'F.col("{grouped[ref]}")'
        return f'F.col("{ref[0]}.{ref[1]}")'

    @EXPRESSIONS("variable")
    def expr_variable(self, node: Node, grouped: dict) -> str:
        return self.parameter_value(node)

    @EXPRESSIONS("null")
    def expr_null(self, node: Node, grouped: dict) -> str:
        found = spark_types.mapping(node.value_type)
        if found is None:
            raise Unsupported(node, f"a NULL of type {node.type} has no Spark type to cast to")
        return f'F.lit(None).cast("{found.ddl}")'

    @EXPRESSIONS("literal")
    def expr_literal(self, node: Node, grouped: dict) -> str:
        if "secret_setting" in node.attrs:
            return self.secret(node)
        if node.value_type.name not in ("integer", "boolean", "string"):
            raise Unsupported(node, f"a literal of type {node.type} has no PySpark generation yet")
        return f"F.lit({node.attrs['value']!r})"

    @EXPRESSIONS("call_scalar_function")
    def expr_call_scalar_function(self, node: Node, grouped: dict) -> str:
        function = node.attrs["function"]
        if function not in self.dependencies.generated:
            raise Unsupported(node, f"{function} is a function that is not generated in this project, so there "
                                    "is nothing to import")
        local = self.callee(function, "scalar_function")
        return f"{local}({', '.join(['ss'] + [self.expr(x, grouped) for x in self.arguments(node)])})"

    @EXPRESSIONS("implicit_convert")
    def expr_implicit_convert(self, node: Node, grouped: dict) -> str:
        args = self.arguments(node)
        source = args[0].value_type
        if node.attrs.get("fits"):
            raise Unsupported(node, "a store that cuts or rounds its value is reproduced by the support "
                                    "library's typed assignment, which is not built yet")
        if source.name in ("integer", "boolean") and node.value_type.name == "integer":
            # A bit converts to 1 or 0, as a boolean cast does; an integer to a wider or narrower integer casts.
            return f'{self.expr(args[0], grouped)}.cast("{spark_type(node.value_type, node)}")'
        raise Unsupported(node, f"the conversion of {source} to {node.type} has no PySpark generation yet")

    def refuse_money(self, node: Node) -> NoReturn:
        """The target has no money type yet: money arithmetic is manual work, whatever the node's own type."""
        raise Unsupported(node, f"{self.differences.difference(MONEY)}; {MONEY_NOT_BUILT}",
                          (untranslatable.NUMBER_HELPERS,))

    def refuse_money_typed(self, node: Node) -> None:
        """A sum, an average or a sum of two values of type money is money arithmetic too."""
        if node.type is not None and node.type.name == "money":
            self.refuse_money(node)

    @EXPRESSIONS("money_multiply", "money_divide")
    def expr_money_arithmetic(self, node: Node, grouped: dict) -> str:
        self.refuse_money(node)

    @EXPRESSIONS("add", "subtract")
    def expr_add_subtract(self, node: Node, grouped: dict) -> str:
        self.refuse_money_typed(node)
        return self.integer_arithmetic(node, grouped)

    @EXPRESSIONS("multiply", "modulo")
    def expr_multiply_modulo(self, node: Node, grouped: dict) -> str:
        return self.integer_arithmetic(node, grouped)

    def integer_arithmetic(self, node: Node, grouped: dict) -> str:
        k = node.kind
        if node.value_type.name != "integer":
            raise Unsupported(node, f"{k} on {node.type} has no PySpark generation yet")
        op = {"add": "+", "subtract": "-", "multiply": "*", "modulo": "%"}[k]
        a, b = self.operands(node, grouped)
        return f'({a} {op} {b}).cast("{spark_type(node.value_type, node)}")'

    @EXPRESSIONS("integer_divide")
    def expr_integer_divide(self, node: Node, grouped: dict) -> str:
        if node.value_type.param("bits") == 64:
            raise Unsupported(node, "bigint division needs an exact integer-division helper, not built yet",
                              (untranslatable.NUMBER_HELPERS,))
        a, b = self.operands(node, grouped)
        # A quotient of integers within int is exact in a double, so the cast truncates
        # toward zero exactly as SQL Server does; dividing by zero raises in both.
        return f'({a} / {b}).cast("{spark_type(node.value_type, node)}")'

    @EXPRESSIONS("count_values")
    def expr_count_values(self, node: Node, grouped: dict) -> str:
        if node.attrs.get("rule"):
            raise Unsupported(node, f"COUNT(DISTINCT) over strings: {STRING_COMPARISON}",
                              (untranslatable.STRING_HELPERS,))
        fn = "F.count_distinct" if node.attrs.get("distinct") else "F.count"
        return f'{fn}({self.expr(self.arguments(node)[0], {})}).cast("int")'

    @EXPRESSIONS("count_rows")
    def expr_count_rows(self, node: Node, grouped: dict) -> str:
        return 'F.count(F.lit(1)).cast("int")'

    @EXPRESSIONS("sum", "average")
    def expr_sum_average(self, node: Node, grouped: dict) -> str:
        k = node.kind
        self.refuse_money_typed(node)
        if node.value_type.param("bits") != 32:
            raise Unsupported(node, f"{k} returning {node.type} is not generated yet")
        a = self.expr(self.arguments(node)[0], {})
        self.draft.caveats[node.id] = SUM_INT
        if k == "sum":
            return f'F.sum({a}).cast("int")'
        # AVG over int is the int sum divided by the count of values, truncated toward zero.
        return f'(F.sum({a}).cast("int") / F.count({a})).cast("int")'

    @EXPRESSIONS("minimum", "maximum")
    def expr_minimum_maximum(self, node: Node, grouped: dict) -> str:
        k = node.kind
        if node.attrs.get("rule"):
            raise Unsupported(node, f"{k} over strings: {STRING_COMPARISON}", (untranslatable.STRING_HELPERS,))
        return f"F.{'min' if k == 'minimum' else 'max'}({self.expr(self.arguments(node)[0], {})})"

    @EXPRESSIONS("null_replacement")
    def expr_null_replacement(self, node: Node, grouped: dict) -> str:
        a, b = self.operands(node, grouped)
        return f'F.coalesce({a}, {b}.cast("{spark_type(node.value_type, node)}"))'

    @EXPRESSIONS("null_if_equal")
    def expr_null_if_equal(self, node: Node, grouped: dict) -> str:
        if node.attrs.get("rule"):
            raise Unsupported(node, f"NULLIF over strings: {STRING_COMPARISON}", (untranslatable.STRING_HELPERS,))
        a, b = self.operands(node, grouped)
        return f"F.nullif({a}, {b})"

    @EXPRESSIONS("compare")
    def expr_compare(self, node: Node, grouped: dict) -> str:
        if node.attrs.get("rule"):
            raise Unsupported(node, STRING_COMPARISON, (untranslatable.STRING_HELPERS,))
        a, b = self.operands(node, grouped)
        return f"({a} {COMPARE[node.attrs['operator']]} {b})"

    @EXPRESSIONS("and", "or")
    def expr_and_or(self, node: Node, grouped: dict) -> str:
        a, b = self.operands(node, grouped)
        return f"({a} {'&' if node.kind == 'and' else '|'} {b})"

    @EXPRESSIONS("not")
    def expr_not(self, node: Node, grouped: dict) -> str:
        return f"(~{self.expr(self.arguments(node)[0], grouped)})"

    @EXPRESSIONS("is_null")
    def expr_is_null(self, node: Node, grouped: dict) -> str:
        return f"{self.expr(self.arguments(node)[0], grouped)}.isNull()"

    def parameter_value(self, node: Node) -> str:
        """A function's parameter read as a column: `F.lit` of a value, or of a column, which it returns as it is."""
        python = self.params.get(str(node.attrs["name"]).casefold())
        if python is None:
            raise Unsupported(node, f"{node.attrs['name']} is not a parameter of this function")
        return f'F.lit({python}).cast("{spark_type(node.value_type, node)}")'

    def secret(self, node: Node) -> str:
        """A literal inside a marked secret is the value of its named setting, read by session state."""
        settings = node.attrs.get("secret_settings") or [node.attrs["secret_setting"]]
        if len(settings) > 1:
            raise Unsupported(node, f"the literal holds {len(settings)} marked secrets ({', '.join(settings)}): one "
                                    "setting cannot stand for all of them")
        self.draft.settings_used.append(settings[0])
        code = f'F.lit(ss.setting("{settings[0]}"))'
        if node.value_type.name == "string":
            return code
        return f'{code}.cast("{spark_type(node.value_type, node)}")'

    @EXPRESSIONS("case")
    def case(self, node: Node, grouped: dict) -> str:
        children = node.children
        code = ""
        otherwise = None
        for i, child in enumerate(children):
            if child.role == "else":
                otherwise = self.expr(child, {})
            elif child.role == "when":
                then = children[i + 1]
                code += ("F.when" if not code else ".when") + f"({self.expr(child, {})}, {self.expr(then, {})})"
        return code + (f".otherwise({otherwise})" if otherwise is not None else "")


def require_accounted(findings: Sequence[untranslatable.Finding], manual: Sequence[ManualItem],
                      artefact: Artefact) -> None:
    """Every construct detected before generation must be under a manual item's replaced subtree. One that is
    not would have been dropped from the file without a word, so it is an error."""
    nodes = {n.id: n for n in artefact.nodes()}
    replaced = {n.id for m in manual for n in nodes[m.replaces].walk()}
    dropped = [f for f in findings if f.node not in replaced]
    if dropped:
        raise DroppedConstruct("; ".join(f"{artefact.identity}: node {f.node} ({f.kind}) would be dropped: {f.reason}"
                                         for f in dropped))


def _shallow(node: Node) -> Node:
    """The node alone, without its subtree, for linking a line to one plan step."""
    return dataclasses.replace(node, children=[])


def generate(artefact: Artefact, snapshot_id: str, tool_version: str, differences: Catalogue, names: Names,
             dependencies: Dependencies | None = None, effects: Callable[[Node], Effect] | None = None) -> GeneratedFile:
    """`names` holds the recorded name adjustments: files, modules and tables are named by them. `dependencies`
    says what the artefact reads and which of that is generated into the same project. `effects` is the source's
    answer to what a statement leaves in `@@ROWCOUNT` and `@@ERROR`; without it no statement of a procedure, batch or
    trigger is generated."""
    return Generator(artefact, snapshot_id, tool_version, differences, names, dependencies or Dependencies(),
                     effects).generate()


def check(artefact: Artefact, generated: GeneratedFile) -> layer1.Layer1Result:
    """Layer 1 for a generated file, with the inputs the representation and generation give it."""
    spans = {n.id: (str(n.span) if n.span else None) for n in artefact.nodes()}
    return layer1.run(generated.text, links=generated.links, node_spans=spans,
                      manual_items=[m.id for m in generated.manual_items],
                      escape_hatch_uses=generated.escape_hatch_uses, expressions=generated.expressions,
                      expression_nodes=generated.expression_nodes, project_package=PROJECT_PACKAGE,
                      variables=generated.variables, readable=generated.readable)
