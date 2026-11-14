"""An artefact that does not parse (spec section 4, item 5, and section 5): it is divided at the boundaries a lexer can
still find, `GO` separators and statement terminators (`;`), and each part becomes an opaque fragment, so no
artefact is rejected for its size or for one error in it.

The lexer reads strings and comments, so a semicolon inside either ends nothing, and a `GO` ends a batch only on a
line of its own (with at most a count and a comment after it). A comment on the line of a terminator stays with the
statement it follows; a comment after the last terminator stays with the last fragment; text is never dropped, and
only whitespace lies between fragments.

With nothing parsed, nothing is known of what a fragment reads or writes: each is a scope barrier, says so, and the
artefact records that its parameters, variables and dependencies beyond the catalog's are unknown. A parse error that
lies inside a fragment is named in that fragment's reason; the others are named in the artefact's assumptions.

The body is the one the artefact's type has, with the fragments as its statements: a script has one `batch` per `GO`
(a `GO` with a count, which runs its batch that many times, is kept as the batch's `separator`), and a view or an
inline function, which is one query with no boundary a lexer can find, is one fragment, its `query`.
"""
from __future__ import annotations

import bisect
import dataclasses

from ...core import scopes, secrets, vocabulary
from ...core.catalog import CatalogReader
from ...core.ir import UNKNOWN, Artefact, Node, SourceSpan
from . import parser
from .extractor import SOURCE

QUERY_TYPES = ("view", "inline_function")
BODIES = {"view": "view_body", "inline_function": "table_function_body", "table_function": "table_function_body",
          "scalar_function": "scalar_function_body", "procedure": "procedure_body", "trigger": "trigger_body",
          "query_batch": "batch_script_body"}
REASON = ("the artefact did not parse, so it was divided at the boundaries a lexer can find (GO separators and "
          "statement terminators) and this part is kept as written")
UNKNOWNS = ("The artefact did not parse, so it was divided at lexer boundaries into fragments kept as written. Its "
            "parameters, its variables and what each fragment reads and writes are not known, so every fragment is a "
            "scope barrier. Its dependencies are only those the catalog holds, and its comparisons against error "
            "numbers are not flagged.")


@dataclasses.dataclass
class Piece:
    start: int
    end: int


@dataclasses.dataclass
class Batch:
    pieces: list[Piece]
    separator: str | None = None          # the GO line that ended it, when it is more than GO
    separator_start: int = 0


def _separator_end(tokens: list[parser.Token], i: int, text: str) -> tuple[int, int, int] | None:
    """If the `GO` token at `i` stands on a line of its own (a count and a comment may follow), the offset where the
    separator ends (after its count), the offset where the line ends and the index of the token after it; None if it
    does not."""
    j = i
    while j > 0 and tokens[j - 1].kind == "space" and "\n" not in tokens[j - 1].text:
        j -= 1
    if j > 0 and not (tokens[j - 1].kind == "space" and "\n" in tokens[j - 1].text):
        return None
    k, sep_end = i + 1, tokens[i].end
    while k < len(tokens):
        t = tokens[k]
        if t.kind == "end":
            return sep_end, len(text), k
        if t.kind == "space" and "\n" in t.text:
            return sep_end, t.offset + t.text.index("\n"), k
        if not (t.kind in ("space", "comment") or (t.kind == "other" and t.text.isdigit())):
            return None
        if t.kind == "other":
            sep_end = t.end
        k += 1
    return sep_end, len(text), k


def divide_text(tokens: list[parser.Token], text: str) -> list[Batch]:
    """The batches of the text and the pieces in each, from the lexer's tokens alone."""
    batches: list[Batch] = []
    pieces: list[Piece] = []
    start: int | None = None
    end = 0
    has_code = False

    def settle() -> None:
        """The text since the last terminator, when nothing ended it: code is a piece; only comments belong to the
        piece before them, or are a piece if nothing else is in the batch."""
        nonlocal start, has_code
        if start is not None:
            if has_code or not pieces:
                pieces.append(Piece(start, end))
            else:
                pieces[-1].end = end
        start, has_code = None, False

    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.kind in ("space", "end"):
            i += 1
            continue
        if t.kind == "go":
            found = _separator_end(tokens, i, text)
            if found is not None:
                settle()
                sep_end, _, i = found
                line = text[t.offset:sep_end].strip()
                batches.append(Batch(pieces, None if line.upper() == "GO" else line, t.offset))
                pieces = []
                continue
        if start is None:
            start = t.offset
        end = t.end
        has_code = has_code or t.kind != "comment"
        i += 1
        if t.kind == "semicolon":
            while i < len(tokens):                 # a comment on the same line stays with the statement it follows
                k = i + 1 if tokens[i].kind == "space" and "\n" not in tokens[i].text else i
                if k < len(tokens) and tokens[k].kind == "comment":
                    end, i = tokens[k].end, k + 1
                else:
                    break
            pieces.append(Piece(start, end))
            start, has_code = None, False
    settle()
    if pieces:
        batches.append(Batch(pieces))
    return batches


class _Lines:
    def __init__(self, text: str):
        self.starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]

    def position(self, offset: int) -> tuple[int, int]:
        line = bisect.bisect_right(self.starts, offset)
        return line, offset - self.starts[line - 1] + 1

    def span(self, start: int, end: int) -> SourceSpan:
        (a, b), (c, d) = self.position(start), self.position(end)
        return SourceSpan(a, b, c, d, start, end - start)


def lower_unparsed(reader: CatalogReader, identity: str, text: str, compatibility_level: int, quoted_identifier: bool,
                   errors: list[parser.ParseError]) -> Artefact:
    """The artefact of a text that did not parse (`errors` are what the parser reported)."""
    artefact_type = reader.kind_of(identity)
    if artefact_type not in BODIES:
        raise ValueError(f"{identity} is a {artefact_type}, which has no body to divide")
    marked = [secrets.SecretSpan.from_json(s) for s in reader.value(identity, "secrets") if s["fact"] == "source_text"]
    lines = _Lines(text)
    batches = divide_text(parser.tokens(text, compatibility_level, quoted_identifier), text)
    settings: set[str] = set()
    placed: set[int] = set()

    ordered = secrets.merged(marked)

    def quoted(message: str) -> str:
        """A parser message quotes the source near the error: a marked secret in it is masked like the text is."""
        for index, span in enumerate(ordered):
            value = text[span.start:span.end]
            if value and value in message:
                name = secrets.setting_name(identity, index)
                message = message.replace(value, secrets.placeholder(name))
                settings.add(name)
        return message

    def fragment(piece: Piece, role: str) -> Node:
        span = lines.span(piece.start, piece.end)
        inside = [e for e in errors if (span.start_line, span.start_column) <= (e.line, e.column)
                  <= (span.end_line, span.end_column)]
        placed.update(id(e) for e in inside)
        reason = REASON + "".join(f"; the parser reported at L{e.line}:{e.column}: {quoted(e.message)}" for e in inside)
        shown, used = secrets.mask(text[piece.start:piece.end], marked, prefix=identity, offset=piece.start)
        settings.update(used)
        return Node("opaque_fragment", span, UNKNOWN, {"reason": reason, "text": shown, "scope_barrier": True,
                                                       "references": []}, role=role)

    attrs: dict = {"options": [], "ansi_nulls": bool(reader.value(identity, "ansi_nulls", SOURCE)),
                   "quoted_identifier": bool(reader.value(identity, "quoted_identifier", SOURCE)), "name": identity}
    assumptions = [UNKNOWNS]
    whole = lines.span(0, len(text))
    if artefact_type == "query_batch":
        children = []
        for batch in batches:
            if not batch.pieces and batch.separator is None:
                continue
            statements = [fragment(p, "statement") for p in batch.pieces]
            span = (lines.span(batch.pieces[0].start, batch.pieces[-1].end) if batch.pieces
                    else lines.span(batch.separator_start, batch.separator_start + len(batch.separator or "")))
            node = Node("batch", span, None, {} if batch.separator is None else {"separator": batch.separator},
                        statements, role="batch")
            children.append(node)
    else:
        pieces = [p for b in batches for p in b.pieces]
        if len(batches) > 1:
            assumptions.append(f"The text holds {len(batches) - 1} GO separator(s), which a {artefact_type} cannot "
                               "have: its fragments are kept in order as one body.")
        if artefact_type in QUERY_TYPES and pieces:
            pieces = [Piece(pieces[0].start, pieces[-1].end)]
        children = [fragment(p, "query" if artefact_type in QUERY_TYPES else "statement") for p in pieces]
    if artefact_type == "trigger":
        attrs.update(table=str(reader.value(identity, "table")), timing=reader.value(identity, "timing"),
                     events=list(reader.value(identity, "events")))
    if artefact_type == "inline_function":
        attrs["form"] = "inline"
    elif artefact_type == "table_function":
        attrs["form"] = "multi_statement"
    body = Node(BODIES[artefact_type], whole, None, attrs, children)

    unplaced = [e for e in errors if id(e) not in placed]
    if unplaced:
        assumptions.append("The parser also reported, outside every fragment: " +
                           "; ".join(f"L{e.line}:{e.column} {quoted(e.message)}" for e in unplaced))
    dependencies = set()
    if reader.has_fact(identity, "references"):
        dependencies |= {r["resolved"] for r in reader.value(identity, "references") if r.get("resolved")}
    if artefact_type == "trigger":
        dependencies.add(attrs["table"])
    dependencies.discard(identity)
    artefact = Artefact(identity, artefact_type, text, body, dependencies=sorted(dependencies),
                        vocabulary_version=vocabulary.version(), secret_settings=sorted(settings),
                        assumptions=sorted(assumptions))
    artefact.number_nodes()
    scopes.annotate(body)
    return artefact
