"""The draft of a generated file: everything generation has put into it so far, and giving up a construct.

Generation writes code for a construct and may find, part way through, that the target cannot generate it exactly
(`Unsupported`). The construct then becomes a manual item, and the file must look as if its code had never been
begun: no line, no import of a function it called, no name it reserved, no setting it read, no caveat for an
operation it would have performed. That holds for the draft by construction. Everything generation puts in the file
is a field of the draft, and `attempt` takes a copy of every field before it runs `produce` and gives every field back if
`produce` raises `Unsupported`. A new field is covered the moment it exists; nothing keeps a list of fields to
restore (issue #106).

Two rules keep that true. A field is a list, dict or set (or a plain value), and what it holds is replaced, never
changed in place: the copy is one level deep, which is what keeps an attempt's cost in step with the file's size
instead of making a wide select list quadratic. And a caller does not keep a field's container across an attempt:
a give-up puts back the copy, so the container it held is no longer the draft's.

What stands in the file where the construct was (a column that fails when it is used, a function that raises when
it is called) is the caller's to emit after the give-up, with `fail_when_called` or its own code.
"""
from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable, Sequence
from typing import TypeVar

from ...core.ir import Node
from . import layer1

T = TypeVar("T")


class Unsupported(Exception):
    """The target cannot generate `node` exactly. `reason` is plain English and may reach a client's report, so it
    names no ticket. `waits_for` names what the construct waits for, in references such as `"#87"`, when it is
    manual work only for want of something not built yet: a release gate and the ticket that builds it search by
    it. A construct that is manual work for good waits for nothing."""

    def __init__(self, node: Node, reason: str, waits_for: Sequence[str] = ()):
        self.node, self.reason, self.waits_for = node, reason, tuple(waits_for)
        super().__init__(reason)


@dataclasses.dataclass(frozen=True)
class ManualItem:
    id: str
    node: str                 # the node the reason is about
    output: str | None        # the output column that fails instead, if one does
    reason: str
    source_position: str
    replaces: str             # the root of the subtree whose code the file replaces with a failure
    waits_for: tuple[str, ...] = ()   # the tickets it is manual work for want of, such as ("#87",); empty for good


@dataclasses.dataclass(frozen=True)
class GivenUp:
    """What `attempt` returns when the construct could not be generated: its manual item, already recorded."""
    item: ManualItem


class Lines:
    """Code lines, each linked to the nodes it came from, and where each linked expression sits."""

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

    def append(self, block: "Lines") -> None:
        """Add another block after this one's lines, carrying its links and spans."""
        offset = len(self.lines)
        self.lines += block.lines
        self.links |= {n + offset: ids for n, ids in block.links.items()}
        self.spans |= {i: layer1.Span(s.line + offset, s.start, s.end) for i, s in block.spans.items()}


class Draft(Lines):
    def __init__(self):
        super().__init__()
        self.imports: dict[str, str] = {}              # imported name -> its import line
        self.taken: set[str] = {"ss", "spark", "F", "DataFrame", "Column", "RuntimeError"}   # Python names in the file
        self.settings_used: list[str] = []             # the named settings the code reads
        self.caveats: dict[str, str] = {}              # node id -> the silent difference its code carries
        self.manual: list[ManualItem] = []
        self.assumptions: list[str] = []               # what generation assumed, each worded for the report
        self.fails_when_called = False                 # the function raises when called: all of it is manual work

    # ------------------------------------------------------------------ giving up

    def attempt(self, produce: Callable[[], T], *, output: Node | None = None,
                replaces: Node | None = None) -> T | GivenUp:
        """Run `produce`. If it raises `Unsupported`, the draft is as it was before (every field), one manual item
        records why (`output` is the column that fails in place of the construct, `replaces` the root of the subtree
        whose code the file does not hold; by default the construct itself), and `GivenUp` is returned. Attempts
        nest: an inner give-up is a manual item inside the outer attempt, and an outer give-up discards it."""
        saved = {name: copy.copy(value) for name, value in vars(self).items()}
        try:
            return produce()
        except Unsupported as unsupported:
            vars(self).update(saved)
            where = unsupported.node.span
            return GivenUp(self._record(f"{unsupported.reason} (source {where})", output or unsupported.node,
                                     output.id if output else None, str(where),
                                     (output or replaces or unsupported.node).id, unsupported.waits_for))

    def manual_item_at(self, node: Node, reason: str, waits_for: Sequence[str] = ()) -> ManualItem:
        """A manual item for a construct that was never attempted, such as a statement of a procedure."""
        text = " ".join(reason.split())
        return self._record(f"{node.kind}: {text} (source {node.span})", node, None, str(node.span), node.id,
                            waits_for)

    def _record(self, what: str, node: Node, output: str | None, source_position: str, replaces: str,
                waits_for: Sequence[str]) -> ManualItem:
        number = len(self.manual) + 1
        item = ManualItem(f"M{number}", node.id, output, f"Manual work {number}: {what}", source_position, replaces,
                          tuple(waits_for))
        self.manual.append(item)
        return item

    def fail_when_called(self, message: str, link: Sequence[Node]) -> None:
        """The function raises when called, with `message`: nothing of it runs."""
        self.emit(f"    raise RuntimeError({message!r})", link)
        self.fails_when_called = True
