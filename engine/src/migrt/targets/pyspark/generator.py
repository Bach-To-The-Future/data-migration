"""Generation for the PySpark target, first cut (spec sections 5 and 6).

Deterministic: the same representation always gives the same file. A view becomes a
function of session state returning a DataFrame. ANSI mode is pinned in every function.
Every code line is linked to the nodes it came from, and so to their source positions.

An operation this target cannot yet generate exactly is declared unsupported here and
becomes a manual item: its column is emitted as `F.raise_error(...)`, so the file still
runs its other columns and fails loudly, never silently, where the item is used. The
report lists every manual item with its reason and source position.

Where a reason or caveat comes from a silent difference, this target names the entry and
its own reason only; the wording of the difference comes from the core's catalogue, so
the source's behaviour is worded by the source half (ADR 0019).
"""
from __future__ import annotations

import dataclasses
import keyword
import re
from collections.abc import Sequence

from ...core.differences import Catalogue
from ...core.ir import Artefact, Node, VType
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


class Unsupported(Exception):
    def __init__(self, node: Node, reason: str):
        self.node, self.reason = node, reason
        super().__init__(reason)


@dataclasses.dataclass(frozen=True)
class ManualItem:
    id: str
    node: str
    output: str | None
    reason: str
    source_position: str


@dataclasses.dataclass
class GeneratedFile:
    relative_path: str
    text: str
    links: dict[int, list[str]]           # generated line number -> node ids
    manual_items: list[ManualItem]
    differences: dict[str, str]           # node id -> the silent difference it carries as a caveat
    escape_hatch_uses: list[str]          # node ids generated as spark.sql (none yet)
    name_adjustments: dict[str, dict[str, str]]   # rule -> {source name: the changed name this file uses}


def spark_type(t: VType, node: Node) -> str:
    if t.name == "integer":
        bits = t.int_param("bits")
        if not t.param("signed"):
            raise Unsupported(node, "tinyint values are SMALLINT in Spark with their 0 to 255 range enforced by the "
                                    "support library, which is not built yet")
        return {16: "smallint", 32: "int", 64: "bigint"}[bits]
    if t.name == "money":
        return f"decimal({t.param('precision')},4)"
    if t.name == "decimal":
        return f"decimal({t.param('precision')},{t.param('scale')})"
    if t.name == "boolean":
        return "boolean"
    raise Unsupported(node, f"no PySpark type mapping for {t} yet")


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

    def emit(self, text: str, nodes: Sequence[Node] = ()) -> None:
        self.lines.append(text)
        ids = sorted({n.id for node in nodes for n in node.walk()}, key=lambda i: int(i[1:]))
        if ids:
            self.links[len(self.lines)] = ids

    def comments(self, node: Node, indent: str) -> None:
        for comment in node.comments:
            for line in comment.splitlines():
                self.emit(f"{indent}# original: {line.strip()}")


class ViewGenerator:
    def __init__(self, artefact: Artefact, snapshot_id: str, tool_version: str, differences: Catalogue,
                 names: Names):
        self.artefact = artefact
        self.recorded_names = names
        self.differences = differences
        self.snapshot_id = snapshot_id
        self.tool_version = tool_version
        self.out = _Lines()
        self.manual: list[ManualItem] = []
        self.caveats_by_node: dict[str, str] = {}
        self.names: set[str] = {"ss", "spark", "F", "DataFrame"}   # Python names taken in the file

    def generate(self) -> GeneratedFile:
        a = self.artefact
        path, name = self.recorded_names.module(a.identity, a.artefact_type)
        self.names.add(name)
        emit = self.out.emit
        emit(f'"""{a.identity}, a view, generated as PySpark by migrt {self.tool_version}.')
        emit("")
        emit(f"Generated from catalog snapshot {self.snapshot_id}. Never edit this file by hand: the tool")
        emit('regenerates it. Comments marked "original:" are carried unchanged from the source.')
        emit('"""')
        emit("from pyspark.sql import DataFrame")
        emit(FUNCTIONS_IMPORT)
        emit("")
        emit("")
        emit(f"def {name}(ss) -> DataFrame:")
        emit(f'    """The view {a.identity}. `ss` is the session state."""')
        emit(f"    {SESSION_SPARK}")
        emit(f"    {ANSI_PIN}")
        self.out.comments(a.body, "    ")
        query = a.body.child("query")
        try:
            result = self.plan(query)
        except Unsupported as unsupported:
            item = self.manual_item(unsupported, None)
            # Raised on the call, not on evaluation: an empty result would hide the failure.
            emit(f"    # source {query.span}")
            emit(f"    raise RuntimeError({item.reason!r})", [a.body])
        else:
            emit(f"    return {result}", [a.body])
        self.drop_unused_functions_import()
        return GeneratedFile(path, "\n".join(self.out.lines) + "\n", self.out.links, self.manual,
                             self.caveats_by_node, [], {rule: dict(sorted(used.items())) for rule, used in sorted(self.recorded_names.used.items())})

    def drop_unused_functions_import(self) -> None:
        """A function body with no column expression (one wholly manual) needs no `F`."""
        line = self.out.lines.index(FUNCTIONS_IMPORT) + 1
        if any("F." in text for text in self.out.lines[line:]):
            return
        del self.out.lines[line - 1]
        self.out.links = {(n - 1 if n > line else n): ids for n, ids in self.out.links.items()}

    def manual_item(self, unsupported: Unsupported, output: Node | None) -> ManualItem:
        node = output or unsupported.node
        item = ManualItem(f"M{len(self.manual) + 1}", node.id, output.attrs["output_name"] if output else None,
                          f"Manual work {len(self.manual) + 1}: {unsupported.reason} (source {unsupported.node.span})",
                          str(unsupported.node.span))
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
        if node.kind == "read_table" and node.attrs.get("named_query"):
            raise Unsupported(node, "reading a named query has no PySpark generation yet")
        if node.kind == "read_table":
            var = python_name(node.attrs["alias"], self.names)
            self.step(node, f'{var} = spark.read.table("{self.recorded_names.table(node.attrs["table"])}").alias("{node.attrs["alias"]}")')
            return var
        if node.kind == "join":
            if node.attrs["kind"] == "cross" or not node.children_with("condition"):
                raise Unsupported(node, "a cross join has no PySpark generation yet")
            left, right = self.plan(node.child("left")), self.plan(node.child("right"))
            condition = self.expr(node.child("condition"), {})
            var = python_name("joined", self.names)
            self.step(node, f'{var} = {left}.join({right}, {condition}, "{node.attrs["kind"]}")')
            return var
        if node.kind == "filter":
            source = self.plan(node.child("input"))
            var = python_name("filtered", self.names)
            self.step(node, f"{var} = {source}.filter({self.expr(node.child('condition'), {})})")
            return var
        if node.kind == "project":
            return self.project(node)
        if node.kind == "aggregate":
            return self.aggregate(node)
        if node.kind == "opaque_fragment":
            raise Unsupported(node, f"{node.attrs['reason']}; lowering assist is not built yet")
        raise Unsupported(node, f"{node.kind} has no PySpark generation yet")

    def step(self, node: Node, code: str) -> None:
        self.out.comments(node, "    ")
        self.out.emit(f"    # source {node.span}")
        self.out.emit(f"    {code}", [node] if node.kind == "read_table" else [_shallow(node)])

    def project(self, node: Node) -> str:
        source = self.plan(node.child("input"))
        var = python_name("projected", self.names)
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
        var = python_name("grouped", self.names)
        self.out.comments(node, "    ")
        self.out.emit(f"    # source {node.span}")
        self.out.emit(f"    {var} = {source}.groupBy(", [_shallow(node)])
        for key in keys:
            ref = (key.attrs["relation"], key.attrs["column"])
            self.out.emit(f'        F.col("{ref[0]}.{ref[1]}").alias("{key_alias[ref]}"),', [key])
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
        selected = python_name("selected", self.names)
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
        """Code for one expression. `grouped` maps (relation, column) of grouping keys to
        their names after grouping; it is empty before grouping."""
        k = node.kind
        args = node.children_with("argument")
        if k == "opaque_fragment":
            raise Unsupported(node, f"{node.attrs['reason']}; lowering assist is not built yet")
        if k == "column":
            ref = (node.attrs["relation"], node.attrs["column"])
            if grouped:
                if ref not in grouped:
                    raise Unsupported(node, "a column outside an aggregate that is not a grouping key")
                return f'F.col("{grouped[ref]}")'
            return f'F.col("{ref[0]}.{ref[1]}")'
        if k == "literal":
            if "secret_setting" in node.attrs:
                raise Unsupported(node, f"the literal is a marked secret, shown as the named setting "
                                        f"{node.attrs['secret_setting']}; nothing reads settings yet (the settings "
                                        "file, issue #44)")
            if node.value_type.name not in ("integer", "boolean", "string"):
                raise Unsupported(node, f"a literal of type {node.type} has no PySpark generation yet")
            return f"F.lit({node.attrs['value']!r})"
        if k == "implicit_convert":
            source = args[0].value_type
            if source.name == "integer" and node.value_type.name == "integer":
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
        raise Unsupported(node, f"{k} has no PySpark generation yet")


def _shallow(node: Node) -> Node:
    """The node alone, without its subtree, for linking a line to one plan step."""
    return dataclasses.replace(node, children=[])


def generate(artefact: Artefact, snapshot_id: str, tool_version: str, differences: Catalogue,
             names: Names) -> GeneratedFile:
    """`names` holds the recorded name adjustments: files, modules and tables are named by them."""
    if artefact.artefact_type != "view":
        raise ValueError(f"{artefact.identity}: only views are generated so far")
    return ViewGenerator(artefact, snapshot_id, tool_version, differences, names).generate()
