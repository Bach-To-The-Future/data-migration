"""Generation for the PySpark target (spec sections 5 and 6).

Deterministic: the same representation always gives the same file. A view and a table-valued function become a
function of session state returning a DataFrame, a scalar function without data access a function of session
state and its arguments returning a column expression. ANSI mode is pinned in every function. Every code line
is linked to the nodes it came from, and so to their source positions, and every expression is emitted as one
contiguous, self-contained span of code that layer 1 checks (`layer1.Span`).

Constructs the target cannot translate are detected first (`untranslatable.detect`), and generation must account
for each of them: an operation this target cannot yet generate exactly is declared unsupported here and becomes a
manual item. Its column is emitted as `F.raise_error(...)`, so the file still runs its other columns and fails
loudly, never silently, where the item is used; a plan step that cannot be generated makes the function raise
when called. A procedure, trigger, batch script, multi-statement table function, or a scalar function that is
not one expression, is procedural generation (issue #50): its file holds the signature and one manual item for
each statement, and the function raises when called. A finding that no manual item covers is `DroppedConstruct`,
an error: nothing is dropped. The report lists every manual item with its reason and source position.

Where a reason or caveat comes from a silent difference, this target names the entry and
its own reason only; the wording of the difference comes from the core's catalogue, so
the source's behaviour is worded by the source half (ADR 0019).
"""
from __future__ import annotations

import ast
import dataclasses
import keyword
import re
from collections.abc import Sequence

from ...core import vocabulary
from ...core.differences import Catalogue
from ...core.ir import Artefact, Node, VType
from . import layer1, untranslatable
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
STRING_COMPARISON = "comparing strings by a named rule needs the support library's comparison helpers (not built yet)"
PROJECT_PACKAGE = "converted"
DESCRIPTIONS = {"view": "a view", "inline_function": "an inline table-valued function",
                "scalar_function": "a scalar function", "table_function": "a multi-statement table-valued function",
                "procedure": "a stored procedure", "trigger": "a trigger", "query_batch": "a query batch script"}
STUB_ISSUE = untranslatable.PROCEDURAL_ISSUE
SPECIAL_CAUSES = {*untranslatable.SPECIAL, untranslatable.STALE, untranslatable.UNSUPPORTED_TYPE}


class Unsupported(Exception):
    def __init__(self, node: Node, reason: str):
        self.node, self.reason = node, reason
        super().__init__(reason)


class DroppedConstruct(Exception):
    """A construct detected before generation that no manual item covers: generation would have dropped it."""


@dataclasses.dataclass(frozen=True)
class ManualItem:
    id: str
    node: str                 # the node the reason is about
    output: str | None        # the output column that fails instead, if one does
    reason: str
    source_position: str
    replaces: str             # the root of the subtree whose code the file replaces with a failure


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


def spark_type(t: VType, node: Node) -> str:
    """The Spark type to cast a value to, where `cast` alone keeps the meaning."""
    found = spark_types.mapping(t)
    if found is None:
        raise Unsupported(node, f"no PySpark type mapping for {t} yet")
    if found.inexact_cast:
        raise Unsupported(node, found.inexact_cast)
    return found.ddl


def python_name(name: str, taken: set[str]) -> str:
    base = re.sub(r"\W", "_", name) or "_"
    if base[0].isdigit():
        base = "_" + base
    candidate, n = base, 2
    while keyword.iskeyword(candidate) or candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    taken.add(candidate)
    return candidate


class _Lines:
    def __init__(self):
        self.lines: list[str] = []
        self.links: dict[int, list[str]] = {}
        self.spans: dict[str, layer1.Span] = {}
        self.pending: list[tuple[str, str]] = []      # (expression node id, its code) not yet placed in a line

    def emit(self, text: str, nodes: Sequence[Node] = ()) -> None:
        self.lines.append(text)
        ids = sorted({n.id for node in nodes for n in node.walk()}, key=lambda i: int(i[1:]))
        if ids:
            self.links[len(self.lines)] = ids
        if not text.lstrip().startswith("#"):
            still = []
            for node_id, code in self.pending:
                at = text.find(code)
                if at < 0:
                    still.append((node_id, code))
                elif node_id not in self.spans:
                    self.spans[node_id] = layer1.Span(len(self.lines), at, at + len(code))
            self.pending = still

    def comments(self, node: Node, indent: str) -> None:
        for comment in node.comments:
            for line in comment.splitlines():
                self.emit(f"{indent}# original: {line.strip()}")

    def mark(self) -> int:
        return len(self.lines)

    def rollback(self, mark: int) -> None:
        """Take back the lines emitted since `mark`: the code of a plan that failed is dead, and leaving it in the
        file would leave variables nothing uses."""
        del self.lines[mark:]
        self.links = {n: ids for n, ids in self.links.items() if n <= mark}
        self.spans = {i: s for i, s in self.spans.items() if s.line <= mark}
        self.pending.clear()

    def append(self, block: "_Lines") -> None:
        """Add another block after this one's lines, carrying its links and spans."""
        offset = len(self.lines)
        self.lines += block.lines
        self.links |= {n + offset: ids for n, ids in block.links.items()}
        self.spans |= {i: layer1.Span(s.line + offset, s.start, s.end) for i, s in block.spans.items()}


class Generator:
    def __init__(self, artefact: Artefact, snapshot_id: str, tool_version: str, differences: Catalogue,
                 names: Names, dependencies: Dependencies):
        self.artefact = artefact
        self.recorded_names = names
        self.differences = differences
        self.dependencies = dependencies
        self.snapshot_id = snapshot_id
        self.tool_version = tool_version
        self.out = _Lines()
        self.manual: list[ManualItem] = []
        self.caveats_by_node: dict[str, str] = {}
        self.taken: set[str] = {"ss", "spark", "F", "DataFrame", "Column", "RuntimeError"}   # Python names in the file
        self.findings = untranslatable.detect(artefact)
        self.detected: dict[str, list[untranslatable.Finding]] = {}
        for found in self.findings:
            self.detected.setdefault(found.node, []).append(found)
        self.imports: dict[str, str] = {}              # imported name -> its import line
        self.params: dict[str, str] = {}               # parameter name, casefolded -> its Python name
        self.parameters: list[dict] = []
        self.settings_used: list[str] = []
        self.assumptions: list[str] = []
        self.functions: list[str] = []
        self.columns: list[dict] | None = None
        self.result = "none"
        self.signature_text: str | None = None
        self.fails = False

    # ------------------------------------------------------------------ the file

    def generate(self) -> GeneratedFile:
        a = self.artefact
        path, name = self.recorded_names.module(a.identity, a.artefact_type)
        self.taken.add(name)
        kind = a.artefact_type
        body = a.body
        if kind == "view":
            self.function_returning_dataframe(name, body, "view")
        elif kind == "inline_function":
            self.function_returning_dataframe(name, body, "table function")
        elif kind == "scalar_function":
            self.scalar_function(name, body)
        elif kind == "table_function":
            self.stub(name, body, "DataFrame", "multi-statement table function", positional=True)
        elif kind == "procedure":
            self.stub(name, body, "None", "procedure", positional=False)
        elif kind == "trigger":
            self.stub(name, body, "None", "trigger", positional=True, extra=["inserted", "deleted"])
        elif kind == "query_batch":
            self.batches(body)
        else:
            raise ValueError(f"{a.identity}: a {kind} is not generated")
        require_accounted(self.findings, self.manual, a)
        return self.assemble(path)

    def assemble(self, path: str) -> GeneratedFile:
        a = self.artefact
        uses = {n.id for n in ast.walk(ast.parse("\n".join(self.out.lines))) if isinstance(n, ast.Name)}
        header = _Lines()
        emit = header.emit
        emit(f'"""{a.identity}, {DESCRIPTIONS[a.artefact_type]}, generated as PySpark by migrt {self.tool_version}.')
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
        for line in sorted(self.imports.values()):
            emit(line)
        emit("")
        emit("")
        header.append(self.out)
        nodes = {n.id: n for n in a.nodes()}
        replaced = {n.id for m in self.manual for n in nodes[m.replaces].walk()}
        expression_nodes = [n.id for n in a.nodes() if vocabulary.part_of(n.kind) == "expression"
                            and n.id not in replaced]
        used = {rule: dict(sorted(names.items())) for rule, names in sorted(self.recorded_names.used.items())}
        return GeneratedFile(path, "\n".join(header.lines) + "\n", header.links, self.manual, self.caveats_by_node, [],
                             used, header.spans, expression_nodes, self.findings, sorted(set(self.settings_used)),
                             self.assumptions, self.functions, self.parameters, self.columns, self.result,
                             self.signature_text or "ss", self.fails)

    def declare_parameters(self, body: Node) -> list[Node]:
        """The parameter nodes, each with its Python name; a default is the caller's to pass (reported)."""
        declared = body.children_with("parameter")
        for p in declared:
            name = p.attrs["name"]
            python = python_name(name.lstrip("@"), self.taken)
            self.params[name.casefold()] = python
            spark = spark_types.mapping(p.type) if p.type is not None else None
            self.parameters.append({"name": name, "python_name": python, "direction": p.attrs["direction"],
                                    "readonly": p.attrs["readonly"], "default": p.attrs["default"],
                                    "type": p.type.to_json() if p.type is not None else None,
                                    "spark_type": spark.ddl if spark else None})
            if p.attrs["default"] is not None:
                self.assumptions.append(
                    f"{self.artefact.identity}: parameter {name} has the default {p.attrs['default']} in the source; "
                    f"the generated function takes it as an argument the caller must pass, so the default is not "
                    f"applied")
        return declared

    def signature(self, declared: Sequence[Node], *, positional: bool, extra: Sequence[str] = ()) -> str:
        names = [self.params[p.attrs["name"].casefold()] for p in declared] + list(extra)
        self.taken.update(extra)
        return ", ".join(["ss"] + ([] if positional or not names else ["*"]) + names)

    def open_function(self, name: str, signature: str, annotation: str, docstring: str, declared: Sequence[Node],
                      *, reads_tables: bool, owner: Node | None = None) -> None:
        """The def line holds the function's own node, where it has one, and its parameters."""
        emit = self.out.emit
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

    def function_returning_dataframe(self, name: str, body: Node, noun: str) -> None:
        a, emit = self.artefact, self.out.emit
        declared = self.declare_parameters(body)
        self.open_function(name, self.signature(declared, positional=True), "DataFrame",
                           f"The {noun} {a.identity}.", declared, reads_tables=True, owner=body)
        self.out.comments(body, "    ")
        query = body.child("query")
        self.result, self.columns = "dataframe", self.result_columns(query)
        before = self.checkpoint()
        try:
            result = self.plan(query)
        except Unsupported as unsupported:
            self.restore(before)
            item = self.manual_item(unsupported, None, replaces=query)
            self.fails = True
            # Raised on the call, not on evaluation: an empty result would hide the failure.
            emit(f"    # source {query.span}")
            emit(f"    raise RuntimeError({item.reason!r})", [body])
        else:
            emit(f"    return {result}", [body])

    def checkpoint(self) -> tuple:
        return (self.out.mark(), dict(self.imports), len(self.settings_used), dict(self.caveats_by_node),
                len(self.manual), set(self.taken))

    def restore(self, checkpoint: tuple) -> None:
        """Back to before a plan that failed: its lines, imports, settings, caveats and manual items are gone."""
        mark, imports, settings, caveats, manual, taken = checkpoint
        self.out.rollback(mark)
        self.imports, self.caveats_by_node, self.taken = imports, caveats, taken
        del self.settings_used[settings:], self.manual[manual:]

    def result_columns(self, query: Node) -> list[dict] | None:
        """The columns the query returns, from its select list; None where the query is not a select."""
        node = query
        while node.kind not in ("project", "aggregate"):
            inputs = node.children_with("input") or node.children_with("left")
            if not inputs:
                return None
            node = inputs[0]
        out = []
        for output in node.children_with("output"):
            found = spark_types.mapping(output.type) if output.type is not None else None
            out.append({"name": output.attrs.get("output_name"),
                        "type": output.type.to_json() if output.type is not None else None,
                        "spark_type": found.ddl if found else None})
        return out

    # ------------------------------------------------------------------ scalar functions

    def scalar_function(self, name: str, body: Node) -> None:
        a, emit = self.artefact, self.out.emit
        statements = body.children_with("statement")
        simple = len(statements) == 1 and statements[0].kind == "return" and statements[0].children_with("argument")
        if not simple:
            self.stub(name, body, "Column", "scalar function", positional=True)
            return
        declared = self.declare_parameters(body)
        self.open_function(name, self.signature(declared, positional=True), "Column",
                           f"The scalar function {a.identity}, a column expression.", declared, reads_tables=False,
                           owner=body)
        self.out.comments(body, "    ")
        ret = statements[0]
        self.result = "column"
        before = self.checkpoint()
        try:
            code = self.expr(ret.child("argument"), {})
        except Unsupported as unsupported:
            self.restore(before)
            item = self.manual_item(unsupported, None, replaces=ret)
            self.fails = True
            emit(f"    # source {ret.span}")
            emit(f"    raise RuntimeError({item.reason!r})", [body])
        else:
            emit(f"    # source {ret.span}")
            emit(f"    return {code}", [ret])

    # ------------------------------------------------------------------ procedural artefacts: reported, not generated

    def stub(self, name: str, body: Node, annotation: str, noun: str, *, positional: bool,
             extra: Sequence[str] = ()) -> None:
        declared = self.declare_parameters(body)
        self.open_function(name, self.signature(declared, positional=positional, extra=extra), annotation,
                           f"The {noun} {self.artefact.identity}.", declared, reads_tables=False, owner=body)
        self.report_block(body, body.children_with("statement"), name, noun, [body])

    def batches(self, body: Node) -> None:
        names = []
        for number, batch in enumerate(body.children_with("batch"), start=1):
            name = python_name(f"batch_{number}", self.taken)
            names.append(name)
            if number > 1:
                self.out.emit("")
                self.out.emit("")
            self.open_function(name, "ss", "None", f"Batch {number} of the script {self.artefact.identity}.", [],
                               reads_tables=False, owner=batch)
            if number == 1:
                self.out.links[len(self.out.lines)] = sorted({*self.out.links.get(len(self.out.lines), []), body.id},
                                                             key=lambda i: int(i[1:]))
            self.report_block(batch, batch.children_with("statement"), name, f"batch {number}", [batch],
                              roots=[batch])
        self.out.emit("")
        self.out.emit("")
        self.out.emit(f"BATCHES = [{', '.join(names)}]")

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
            reasons = [f.reason for f in causes] or [self.statement_reason(container, node)]
            items.append((node, self.manual_item_at(node, "; ".join(reasons))))
        for node, item in items:
            self.out.comments(node, "    ")
            self.out.emit(f"    # {item.reason}")
        self.fails = True
        self.out.emit(f"    raise RuntimeError({self.stub_message(name, noun, len(items))!r})", link)

    @staticmethod
    def statement_reason(container: Node, node: Node) -> str:
        if container.kind == "scalar_function_body":
            return ("this function is more than one expression, so it is not a column expression; raising it to "
                    "one and the Python UDF fallback are not built yet (issue #64)")
        return untranslatable.procedural_reason(node.kind)

    @staticmethod
    def stub_message(name: str, noun: str, count: int) -> str:
        return (f"{name}: {count} parts of this {noun} are manual work (see the Manual work comments in the file); "
                "it cannot run until they are done")

    # ------------------------------------------------------------------ manual work

    def manual_item(self, unsupported: Unsupported, output: Node | None, replaces: Node | None = None) -> ManualItem:
        node = output or unsupported.node
        self.out.pending.clear()           # pieces of the expression that failed are not in the file
        item = ManualItem(f"M{len(self.manual) + 1}", node.id, output.id if output else None,
                          f"Manual work {len(self.manual) + 1}: {unsupported.reason} (source {unsupported.node.span})",
                          str(unsupported.node.span), (output or replaces or unsupported.node).id)
        self.manual.append(item)
        return item

    def manual_item_at(self, node: Node, reason: str) -> ManualItem:
        text = " ".join(reason.split())
        item = ManualItem(f"M{len(self.manual) + 1}", node.id, None,
                          f"Manual work {len(self.manual) + 1}: {node.kind}: {text} (source {node.span})",
                          str(node.span), node.id)
        self.manual.append(item)
        return item

    def manual_column(self, item: ManualItem, output: Node) -> str:
        """A column that fails loudly on any row, typed as the output so its shape holds."""
        code = f"F.raise_error(F.lit({item.reason!r}))"
        try:
            return f'{code}.cast("{spark_type(output.value_type, output)}")'
        except Unsupported:
            return code

    # ------------------------------------------------------------------ plan steps

    def plan(self, node: Node) -> str:
        """Emit the statements for one plan step and return the variable holding it."""
        self.refuse_detected(node)
        for definition in node.children_with("definition"):
            raise Unsupported(definition, untranslatable.reason_for_kind(definition.kind) or "not generated")
        if node.kind == "read_table":
            return self.read_table(node)
        if node.kind == "join":
            if node.attrs["kind"] == "cross" or not node.children_with("condition"):
                raise Unsupported(node, "a cross join has no PySpark generation yet")
            left, right = self.plan(node.child("left")), self.plan(node.child("right"))
            condition = self.expr(node.child("condition"), {})
            var = python_name("joined", self.taken)
            self.step(node, f'{var} = {left}.join({right}, {condition}, "{node.attrs["kind"]}")')
            return var
        if node.kind == "filter":
            source = self.plan(node.child("input"))
            var = python_name("filtered", self.taken)
            self.step(node, f"{var} = {source}.filter({self.expr(node.child('condition'), {})})")
            return var
        if node.kind == "project":
            return self.project(node)
        if node.kind == "aggregate":
            return self.aggregate(node)
        raise Unsupported(node, untranslatable.reason_for_kind(node.kind) or f"{node.kind} has no PySpark generation yet")

    def refuse_detected(self, node: Node) -> None:
        """A node detected before generation as untranslatable is never generated, whatever else is known of it."""
        found = self.detected.get(node.id)
        if found:
            raise Unsupported(node, "; ".join(f.reason for f in found))

    def read_table(self, node: Node) -> str:
        if node.attrs.get("named_query"):
            raise Unsupported(node, "reading a named query has no PySpark generation yet")
        if node.attrs.get("temp_table") or node.attrs.get("row_set"):
            raise Unsupported(node, f"reading a temp table or a table variable is procedural code (issue #{STUB_ISSUE})")
        table, alias = node.attrs["table"], node.attrs["alias"]
        var = python_name(alias, self.taken)
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
        for local, line in self.imports.items():
            if line.startswith(f"from {module} import "):
                return local
        local = python_name(function, self.taken) if function in self.taken else function
        self.taken.add(local)
        self.imports[local] = (f"from {module} import {function}" if local == function
                               else f"from {module} import {function} as {local}")
        return local

    def step(self, node: Node, code: str) -> None:
        self.out.comments(node, "    ")
        self.out.emit(f"    # source {node.span}")
        self.out.emit(f"    {code}", [node] if node.kind == "read_table" else [_shallow(node)])

    def project(self, node: Node) -> str:
        source = self.plan(node.child("input"))
        var = python_name("projected", self.taken)
        self.out.comments(node, "    ")
        self.out.emit(f"    # source {node.span}")
        self.out.emit(f"    {var} = {source}.select(", [_shallow(node)])
        for output in node.children_with("output"):
            self.output_line(output, lambda o: self.expr(o, {}))
        self.out.emit("    )", [_shallow(node)])
        return var

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
                raise Unsupported(key, f"grouping by strings: {STRING_COMPARISON}")
            ref = (key.attrs["relation"], key.attrs["column"])
            exact = [o.attrs["output_name"] for o in outputs if o.kind == "column"
                     and (o.attrs["relation"], o.attrs["column"]) == ref]
            key_alias[ref] = exact[0] if exact else python_name(f"{ref[0]}_{ref[1]}", used)
        var = python_name("grouped", self.taken)
        self.out.comments(node, "    ")
        self.out.emit(f"    # source {node.span}")
        self.out.emit(f"    {var} = {source}.groupBy(", [_shallow(node)])
        for key in keys:
            ref = (key.attrs["relation"], key.attrs["column"])
            code = f'F.col("{ref[0]}.{ref[1]}")'
            self.out.pending.append((key.id, code))
            self.out.emit(f'        {code}.alias("{key_alias[ref]}"),', [key])
        self.out.emit("    ).agg(", [_shallow(node)])
        in_agg = [o for o in outputs if any(n.kind in AGGREGATE_KINDS for n in o.walk())]
        manual_outputs = set()
        for output in in_agg:
            try:
                code = self.expr(output, {})
            except Unsupported as unsupported:
                manual_outputs.add(output.id)
                self.manual_item(unsupported, output)
                continue
            self.out.comments(output, "        ")
            self.out.emit(f'        {code}.alias("{output.attrs["output_name"]}"),', [output])
        if len(manual_outputs) == len(in_agg):
            # Spark's agg needs one expression; a count keeps the groups when every one is manual.
            self.out.emit('        F.count(F.lit(1)).alias("__groups"),', [_shallow(node)])
        self.out.emit("    )", [_shallow(node)])
        selected = python_name("selected", self.taken)
        self.out.emit(f"    {selected} = {var}.select(", [_shallow(node)])
        manual_by_node = {m.node: m for m in self.manual}
        for output in outputs:
            name = output.attrs["output_name"]
            if output.id in manual_by_node:
                self.out.comments(output, "        ")
                self.out.emit(f'        {self.manual_column(manual_by_node[output.id], output)}.alias("{name}"),', [output])
            elif output.id in {o.id for o in in_agg}:
                self.out.emit(f'        F.col("{name}"),', [output])
            else:
                self.output_line(output, lambda o: self.expr(o, key_alias))
        self.out.emit("    )", [_shallow(node)])
        return selected

    def output_line(self, output: Node, generate) -> None:
        name = output.attrs["output_name"]
        try:
            code = generate(output)
        except Unsupported as unsupported:
            code = self.manual_column(self.manual_item(unsupported, output), output)
        self.out.comments(output, "        ")
        self.out.emit(f'        {code}.alias("{name}"),', [output])

    # ------------------------------------------------------------------ expressions

    def expr(self, node: Node, grouped: dict) -> str:
        """Code for one expression, recorded so that layer 1 can check it alone. `grouped` maps (relation,
        column) of grouping keys to their names after grouping; it is empty before grouping."""
        self.refuse_detected(node)
        code = self.expression(node, grouped)
        self.out.pending.append((node.id, code))
        return code

    def expression(self, node: Node, grouped: dict) -> str:
        k = node.kind
        args = node.children_with("argument")
        if k == "column":
            ref = (node.attrs["relation"], node.attrs["column"])
            if grouped:
                if ref not in grouped:
                    raise Unsupported(node, "a column outside an aggregate that is not a grouping key")
                return f'F.col("{grouped[ref]}")'
            return f'F.col("{ref[0]}.{ref[1]}")'
        if k == "variable":
            return self.parameter_value(node)
        if k == "null":
            found = spark_types.mapping(node.value_type)
            return f'F.lit(None).cast("{found.ddl}")' if found else "F.lit(None)"
        if k == "literal":
            if "secret_setting" in node.attrs:
                return self.secret(node)
            if node.value_type.name not in ("integer", "boolean", "string"):
                raise Unsupported(node, f"a literal of type {node.type} has no PySpark generation yet")
            return f"F.lit({node.attrs['value']!r})"
        if k == "case":
            return self.case(node)
        if k == "call_scalar_function":
            function = node.attrs["function"]
            if function not in self.dependencies.generated:
                raise Unsupported(node, f"{function} is a function that is not generated in this project, so there "
                                        "is nothing to import")
            local = self.callee(function, "scalar_function")
            return f"{local}({', '.join(['ss'] + [self.expr(x, grouped) for x in args])})"
        if k == "implicit_convert":
            source = args[0].value_type
            if node.attrs.get("fits"):
                raise Unsupported(node, "a store that cuts or rounds its value is reproduced by the support "
                                        "library's typed assignment, which is not built yet")
            if source.name in ("integer", "boolean") and node.value_type.name == "integer":
                # A bit converts to 1 or 0, as a boolean cast does; an integer to a wider or narrower integer casts.
                return f'{self.expr(args[0], grouped)}.cast("{spark_type(node.value_type, node)}")'
            raise Unsupported(node, f"the conversion of {source} to {node.type} has no PySpark generation yet")
        if k in ("money_multiply", "money_divide") or (node.type is not None and node.type.name == "money"
                                                       and k in ("add", "subtract", "sum", "average")):
            raise Unsupported(node, f"{self.differences.difference(MONEY)}; {MONEY_NOT_BUILT}")
        if k in ("add", "subtract", "multiply", "modulo"):
            if node.value_type.name != "integer":
                raise Unsupported(node, f"{k} on {node.type} has no PySpark generation yet")
            op = {"add": "+", "subtract": "-", "multiply": "*", "modulo": "%"}[k]
            a, b = (self.expr(x, grouped) for x in args)
            return f'({a} {op} {b}).cast("{spark_type(node.value_type, node)}")'
        if k == "integer_divide":
            if node.value_type.param("bits") == 64:
                raise Unsupported(node, "bigint division needs an exact integer-division helper, not built yet")
            a, b = (self.expr(x, grouped) for x in args)
            # A quotient of integers within int is exact in a double, so the cast truncates
            # toward zero exactly as SQL Server does; dividing by zero raises in both.
            return f'({a} / {b}).cast("{spark_type(node.value_type, node)}")'
        if k == "count_values":
            if node.attrs.get("rule"):
                raise Unsupported(node, f"COUNT(DISTINCT) over strings: {STRING_COMPARISON}")
            fn = "F.count_distinct" if node.attrs.get("distinct") else "F.count"
            return f'{fn}({self.expr(args[0], {})}).cast("int")'
        if k == "count_rows":
            return 'F.count(F.lit(1)).cast("int")'
        if k in ("sum", "average"):
            if node.value_type.param("bits") != 32:
                raise Unsupported(node, f"{k} returning {node.type} is not generated yet")
            a = self.expr(args[0], {})
            self.caveats_by_node[node.id] = SUM_INT
            if k == "sum":
                return f'F.sum({a}).cast("int")'
            # AVG over int is the int sum divided by the count of values, truncated toward zero.
            return f'(F.sum({a}).cast("int") / F.count({a})).cast("int")'
        if k in ("minimum", "maximum"):
            if node.attrs.get("rule"):
                raise Unsupported(node, f"{k} over strings: {STRING_COMPARISON}")
            return f"F.{'min' if k == 'minimum' else 'max'}({self.expr(args[0], {})})"
        if k == "null_replacement":
            a, b = (self.expr(x, grouped) for x in args)
            return f'F.coalesce({a}, {b}.cast("{spark_type(node.value_type, node)}"))'
        if k == "null_if_equal":
            if node.attrs.get("rule"):
                raise Unsupported(node, f"NULLIF over strings: {STRING_COMPARISON}")
            a, b = (self.expr(x, grouped) for x in args)
            return f"F.nullif({a}, {b})"
        if k == "compare":
            if node.attrs.get("rule"):
                raise Unsupported(node, STRING_COMPARISON)
            a, b = (self.expr(x, grouped) for x in args)
            return f"({a} {COMPARE[node.attrs['operator']]} {b})"
        if k in ("and", "or"):
            a, b = (self.expr(x, grouped) for x in args)
            return f"({a} {'&' if k == 'and' else '|'} {b})"
        if k == "not":
            return f"(~{self.expr(args[0], grouped)})"
        if k == "is_null":
            return f"{self.expr(args[0], grouped)}.isNull()"
        raise Unsupported(node, untranslatable.reason_for_kind(k) or f"{k} has no PySpark generation yet")

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
        self.settings_used.append(settings[0])
        code = f'F.lit(ss.setting("{settings[0]}"))'
        if node.value_type.name == "string":
            return code
        return f'{code}.cast("{spark_type(node.value_type, node)}")'

    def case(self, node: Node) -> str:
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
             dependencies: Dependencies | None = None) -> GeneratedFile:
    """`names` holds the recorded name adjustments: files, modules and tables are named by them. `dependencies`
    says what the artefact reads and which of that is generated into the same project."""
    return Generator(artefact, snapshot_id, tool_version, differences, names, dependencies or Dependencies()).generate()


def check(artefact: Artefact, generated: GeneratedFile) -> layer1.Layer1Result:
    """Layer 1 for a generated file, with the inputs the representation and generation give it."""
    spans = {n.id: (str(n.span) if n.span else None) for n in artefact.nodes()}
    return layer1.run(generated.text, links=generated.links, node_spans=spans,
                      manual_items=[m.id for m in generated.manual_items],
                      escape_hatch_uses=generated.escape_hatch_uses, expressions=generated.expressions,
                      expression_nodes=generated.expression_nodes, project_package=PROJECT_PACKAGE)
