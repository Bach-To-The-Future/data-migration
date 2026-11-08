"""SQL Server's parser: Microsoft ScriptDom through pythonnet, one parser per
compatibility level (spec sections 16 and 17)."""
from __future__ import annotations

import dataclasses
from typing import Any

from ...core.ir import SourceSpan
from . import dotnet

# Compatibility level -> ScriptDom parser class. Levels 80 and 90 are the unverified preview.
PARSERS = {80: "TSql80Parser", 90: "TSql90Parser", 100: "TSql100Parser", 110: "TSql110Parser",
           120: "TSql120Parser", 130: "TSql130Parser", 140: "TSql140Parser", 150: "TSql150Parser",
           160: "TSql160Parser", 170: "TSql170Parser"}


@dataclasses.dataclass(frozen=True)
class ParseError:
    line: int
    column: int
    message: str


class ParseFailed(Exception):
    def __init__(self, identity: str, errors: list[ParseError]):
        self.errors = errors
        super().__init__(f"{identity} did not parse: " +
                         "; ".join(f"L{e.line}:{e.column} {e.message}" for e in errors))


@dataclasses.dataclass(frozen=True)
class Comment:
    text: str
    offset: int


@dataclasses.dataclass
class Parsed:
    text: str
    tree: Any                       # ScriptDom TSqlScript, a .NET object
    comments: list[Comment]

    def span(self, fragment) -> SourceSpan:
        """A fragment's position, with its end worked out from the text."""
        start, length = int(fragment.StartOffset), int(fragment.FragmentLength)
        end = start + length
        before = self.text[:end]
        end_line = before.count("\n") + 1
        end_column = end - (before.rfind("\n") + 1) + 1
        return SourceSpan(int(fragment.StartLine), int(fragment.StartColumn), end_line, end_column, start, length)


def parse(identity: str, text: str, compatibility_level: int, quoted_identifier: bool) -> Parsed:
    dotnet.load()
    import Microsoft.SqlServer.TransactSql.ScriptDom as scriptdom  # pyright: ignore[reportMissingImports]  (a .NET namespace, loaded by pythonnet at run time)
    from System.IO import StringReader  # pyright: ignore[reportMissingImports]  (a .NET namespace, loaded by pythonnet at run time)

    if compatibility_level not in PARSERS:
        raise ValueError(f"no T-SQL parser for compatibility level {compatibility_level}")
    parser = getattr(scriptdom, PARSERS[compatibility_level])(quoted_identifier)
    tree, errors = parser.Parse(StringReader(text), None)
    if errors is not None and errors.Count:
        raise ParseFailed(identity, [ParseError(e.Line, e.Column, e.Message) for e in errors])
    comment_types = {scriptdom.TSqlTokenType.SingleLineComment, scriptdom.TSqlTokenType.MultilineComment}
    comments = [Comment(str(t.Text), int(t.Offset)) for t in tree.ScriptTokenStream if t.TokenType in comment_types]
    return Parsed(text, tree, comments)


_visitor_class = None
_visits: list = []   # the callbacks of the walks in progress, innermost last


def walk(fragment, visit) -> None:
    """Visit a ScriptDom fragment and every fragment below it. The .NET subclass is defined
    once: pythonnet refuses a second type of the same name."""
    global _visitor_class
    if _visitor_class is None:
        import Microsoft.SqlServer.TransactSql.ScriptDom as scriptdom  # pyright: ignore[reportMissingImports]  (a .NET namespace, loaded by pythonnet at run time)

        class Visitor(scriptdom.TSqlFragmentVisitor):
            __namespace__ = "migrt.parser"

            def Visit(self, node):  # noqa: N802 (ScriptDom's name)
                _visits[-1](node)

        _visitor_class = Visitor
    _visits.append(visit)
    try:
        fragment.Accept(_visitor_class())
    finally:
        _visits.pop()
