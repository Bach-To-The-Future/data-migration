"""Layer 1 of the testing module for the PySpark target: code checks (spec, "The six
layers", and section 17: layer 1's checks belong to the target).

Each check reads the generated file and what generation declared about it (its links to
the representation, its manual items, its escape-hatch uses); none of them runs the code.
A linked expression is one self-contained span (spec section 5, "Generation"): the generated file records where
each expression's code sits, and the span check reads exactly that code alone.
"""
from __future__ import annotations

import ast
import builtins
import dataclasses
import re
import sys

import pyflakes.api
import pyflakes.messages
import pyflakes.reporter

ANSI_KEY, ANSI_VALUE = "spark.sql.ansi.enabled", "true"
STANDARD_MODULES = {"__future__", "dataclasses", "datetime", "decimal", "typing"}
LINT_MESSAGES = (pyflakes.messages.UndefinedName, pyflakes.messages.UnusedVariable,
                 pyflakes.messages.UnusedImport, pyflakes.messages.UndefinedLocal,
                 pyflakes.messages.RedefinedWhileUnused, pyflakes.messages.ImportStarUsed)
WRITE_METHODS = {"write", "writeTo", "saveAsTable", "insertInto", "writeStream"}
MANUAL_MARK = re.compile(r"Manual work (\d+):")


@dataclasses.dataclass(frozen=True)
class Span:
    """Where one expression's code sits in the file: the 1-based line, and the columns [start, end) on it."""
    line: int
    start: int
    end: int


@dataclasses.dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclasses.dataclass(frozen=True)
class Layer1Result:
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.passed]

    def to_json(self) -> dict:
        return {"passed": self.passed, "checks": [dataclasses.asdict(c) for c in self.checks]}


def run(text: str, *, links: dict[int, list[str]], node_spans: dict[str, str | None],
        manual_items: list[str], escape_hatch_uses: list[str], expressions: dict[str, Span],
        expression_nodes: list[str], library_module: str | None = None,
        project_package: str = "generated") -> Layer1Result:
    """links: generated line -> node ids; node_spans: every node id in the artefact's
    representation -> its source position (None if it has none); manual_items: the ids
    ('M1', ...) generation declared; expressions: the span of the code of each expression generation
    emitted; expression_nodes: every expression node whose code is in the file, each of which must have one."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return Layer1Result((Check("parses", False, f"line {exc.lineno}: {exc.msg}"),))
    return Layer1Result((
        Check("parses", True),
        _lint(text),
        _imports(tree, library_module, project_package),
        _ansi(tree),
        _writes(tree),
        _escape_hatch(tree, escape_hatch_uses),
        _positions(tree, text, links, node_spans),
        _manual(text, manual_items),
        _expressions(tree, text, expressions, expression_nodes, node_spans),
    ))


def _lint(text: str) -> Check:
    class Collect(pyflakes.reporter.Reporter):
        def __init__(self):
            super().__init__(sys.stdout, sys.stderr)
            self.found = []

        def flake(self, message):
            if isinstance(message, LINT_MESSAGES):
                self.found.append(str(message))

        def syntaxError(self, filename, msg, lineno, offset, text):  # noqa: N802 (pyflakes' name)
            self.found.append("syntax error")

    reporter = Collect()
    pyflakes.api.check(text, "<generated>", reporter)
    reporter.found += _unreachable(ast.parse(text))
    return Check("lint", not reporter.found, "; ".join(reporter.found))


def _unreachable(tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block[:-1]):
                if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                    found.append(f"line {block[i + 1].lineno}: unreachable code")
                    break
    return found


def _imports(tree: ast.AST, library_module: str | None, project_package: str) -> Check:
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [("." * node.level) + (node.module or "")]
        else:
            continue
        for name in names:
            root = name.split(".")[0]
            allowed = (root in ("pyspark", "delta") or root in STANDARD_MODULES or root == project_package
                       or (library_module is not None and root == library_module))
            if not allowed:
                bad.append(f"line {node.lineno}: {name}")
    return Check("import allow-list", not bad, "; ".join(bad))


def _is_ansi_pin(stmt: ast.stmt) -> bool:
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return False
    call = stmt.value
    return (isinstance(call.func, ast.Attribute) and call.func.attr == "set"
            and isinstance(call.func.value, ast.Attribute) and call.func.value.attr == "conf"
            and [a.value for a in call.args if isinstance(a, ast.Constant)] == [ANSI_KEY, ANSI_VALUE])


def _ansi(tree: ast.Module) -> Check:
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if not functions:
        return Check("ANSI mode pinned", False, "no function in the file")
    missing = []
    for fn in functions:
        body = fn.body[1:] if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant) else fn.body
        # The pin comes before any other work: at most the binding of `spark` precedes it.
        head = body[:2]
        if not any(_is_ansi_pin(s) for s in head):
            missing.append(fn.name)
    pin_lines = {s.lineno for s in ast.walk(tree) if isinstance(s, ast.stmt) and _is_ansi_pin(s)}
    unpinned = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and n.value == ANSI_KEY and n.lineno not in pin_lines]
    detail = "; ".join([f"{m} does not pin ANSI mode first" for m in missing] +
                       [f"line {line}: {ANSI_KEY} set other than by the pin" for line in unpinned])
    return Check("ANSI mode pinned", not detail, detail)


def _writes(tree: ast.AST) -> Check:
    found = [f"line {n.lineno}: .{n.attr}" for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr in WRITE_METHODS]
    return Check("writes through session state", not found, "; ".join(found))


def _escape_hatch(tree: ast.AST, declared: list[str]) -> Check:
    uses = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("sql", "expr")]
    ok = len(uses) == len(declared)
    return Check("escape hatch accounted for", ok,
                 "" if ok else f"{len(uses)} spark.sql or F.expr uses at lines {uses}, {len(declared)} declared and flagged")


def _positions(tree: ast.Module, text: str, links: dict[int, list[str]], node_spans: dict[str, str | None]) -> Check:
    problems = []
    lines = text.splitlines()
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        for stmt in fn.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                continue  # the docstring
            if _is_ansi_pin(stmt) or ast.unparse(stmt) == "spark = ss.spark":
                continue  # the boilerplate every function opens with
            for line in range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1):
                code = lines[line - 1].strip()
                if not code or code.startswith("#"):
                    continue
                if not links.get(line):
                    problems.append(f"line {line} is linked to no node")
    linked = {node for ids in links.values() for node in ids}
    for node, span in node_spans.items():
        if span is None:
            problems.append(f"node {node} has no source position")
        if node not in linked:
            problems.append(f"node {node} is linked from no generated code")
    for ids in links.values():
        problems += [f"linked node {i} is not in the representation" for i in ids if i not in node_spans]
    return Check("source positions", not problems, "; ".join(sorted(set(problems))))


def _manual(text: str, declared: list[str]) -> Check:
    marked = sorted({f"M{m}" for m in MANUAL_MARK.findall(text)}, key=lambda s: int(s[1:]))
    expected = sorted(declared, key=lambda s: int(s[1:]))
    ok = marked == expected
    return Check("manual items accounted for", ok,
                 "" if ok else f"the file marks {marked}; generation declared {expected}")


def _bound_names(tree: ast.Module) -> set[str]:
    """Names the module binds at its top level: imports, functions and classes."""
    bound: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            bound.add(node.name)
    return bound


def _free_names(expression: ast.expr) -> set[str]:
    """Names an expression reads that it does not bind itself (a lambda's or a comprehension's own)."""
    bound = {n.id for n in ast.walk(expression) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    bound |= {a.arg for n in ast.walk(expression) if isinstance(n, ast.Lambda)
              for a in n.args.args + n.args.kwonlyargs}
    return {n.id for n in ast.walk(expression) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)} - bound


def _expressions(tree: ast.Module, text: str, spans: dict[str, Span], expected: list[str],
                 node_spans: dict[str, str | None]) -> Check:
    """Each linked expression is one contiguous span that evaluates alone: it parses as an expression and
    reads nothing but the module's imports and its function's parameters, never a local variable. Two spans
    nest or stand apart, never cross."""
    problems = [f"expression node {node} has no span" for node in expected if node not in spans]
    lines = text.splitlines()
    module_names = _bound_names(tree) | set(dir(builtins))
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    for node, span in sorted(spans.items()):
        if node not in node_spans:
            problems.append(f"expression node {node} is not in the representation")
            continue
        if not 1 <= span.line <= len(lines) or not 0 <= span.start < span.end <= len(lines[span.line - 1]):
            problems.append(f"expression node {node}: its span is not on a line of the file")
            continue
        code = lines[span.line - 1][span.start:span.end]
        owner = next((f for f in functions if f.body and f.lineno < span.line <= (f.end_lineno or f.lineno)), None)
        if owner is None:
            problems.append(f"expression node {node}: its span (line {span.line}) is in no function")
            continue
        try:
            parsed = ast.parse(code, mode="eval")
        except SyntaxError:
            problems.append(f"expression node {node}: line {span.line} columns {span.start}-{span.end} are not one "
                            f"complete expression")
            continue
        if code != code.strip():
            problems.append(f"expression node {node}: its span has surrounding whitespace")
        allowed = module_names | {a.arg for a in owner.args.args + owner.args.kwonlyargs}
        outside = sorted(_free_names(parsed.body) - allowed)
        if outside:
            problems.append(f"expression node {node} reads {', '.join(outside)}, which only the function defines "
                            f"locally, so it cannot be evaluated alone")
    ordered = sorted((s.line, s.start, -s.end, n) for n, s in spans.items())
    for i, (line, start, negative_end, node) in enumerate(ordered):
        end = -negative_end
        for line2, start2, negative_end2, other in ordered[i + 1:]:
            if line2 != line or start2 >= end:
                break
            if -negative_end2 > end:
                problems.append(f"expression nodes {node} and {other} overlap without one holding the other")
    return Check("linked expressions are self-contained spans", not problems, "; ".join(problems))
