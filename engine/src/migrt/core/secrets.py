"""Marked secrets (spec section 3, "Secrets"): spans a source's fixed patterns marked as
likely credentials or connection details.

A marked span stays in the client's own catalog. Wherever text leaves it, for a model, a
report or generated code, the span is masked: replaced by a named setting, which the report
lists. Pattern matching misses some secrets, and the report says so (MISSES_SOME).
"""
from __future__ import annotations

import dataclasses
from typing import Iterable

MISSES_SOME = ("Likely secrets were found by fixed patterns, which miss some: a credential in a form the patterns "
               "do not know is not masked.")


@dataclasses.dataclass(frozen=True, order=True)
class SecretSpan:
    """A marked span of one text: its offset and length, and the pattern that marked it.
    For a raw row, `row` and `column` say which cell the text is."""
    start: int
    length: int
    pattern: str
    row: int | None = None
    column: str | None = None

    @property
    def end(self) -> int:
        return self.start + self.length

    def to_json(self) -> dict:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}

    @staticmethod
    def from_json(value: dict) -> "SecretSpan":
        return SecretSpan(value["start"], value["length"], value["pattern"], value.get("row"), value.get("column"))


def merged(spans: Iterable[SecretSpan]) -> list[SecretSpan]:
    """Overlapping or touching spans of the same text become one, keeping the first pattern."""
    out: list[SecretSpan] = []
    for span in sorted(spans, key=lambda s: (s.row if s.row is not None else -1, s.column or "", s.start, -s.length)):
        last = out[-1] if out else None
        if last and (last.row, last.column) == (span.row, span.column) and span.start <= last.end:
            out[-1] = dataclasses.replace(last, length=max(last.end, span.end) - last.start)
        else:
            out.append(span)
    return out


def setting_name(prefix: str, index: int) -> str:
    """The named setting that stands for the object's index-th marked span (0-based)."""
    return f"{prefix}.secret_{index + 1}"


def placeholder(setting: str) -> str:
    return f"[secret: setting {setting}]"


def mask_cell(text: str | None, marked: list[dict], *, prefix: str, fact: str, row: int | None = None,
              column: str | None = None) -> tuple[str | None, list[str]]:
    """Mask the spans of one cell of an object's fact, such as one job step's command.
    `marked` is the object's whole `secrets` fact, in its stored order, which numbers the
    settings, so a secret has the same setting wherever its cell is shown."""
    if text is None:
        return None, []
    cell = sorted(((index, SecretSpan.from_json(entry)) for index, entry in enumerate(marked)
                   if entry.get("fact") == fact and entry.get("row") == row and entry.get("column") == column),
                  key=lambda pair: pair[1].start)
    pieces, settings, at = [], [], 0
    for index, span in cell:
        start, end = max(span.start, at), min(span.end, len(text))
        if start >= end:
            continue
        name = setting_name(prefix, index)
        pieces += [text[at:start], placeholder(name)]
        settings.append(name)
        at = end
    pieces.append(text[at:])
    return "".join(pieces), settings


def mask(text: str, spans: Iterable[SecretSpan], *, prefix: str, offset: int = 0) -> tuple[str, list[str]]:
    """Mask every marked span that falls in `text`, which starts at `offset` in the text the
    spans were marked in. A span is numbered by its place among all the spans given, so the
    same secret has the same setting wherever a part of the text is masked. A span only
    partly inside `text` masks the part inside. Returns the masked text and the settings used."""
    ordered = merged(spans)
    pieces, settings, at = [], [], 0
    for index, span in enumerate(ordered):
        start, end = max(span.start - offset, 0), min(span.end - offset, len(text))
        if start >= end or end <= at:
            continue
        name = setting_name(prefix, index)
        pieces += [text[at:max(start, at)], placeholder(name)]
        settings.append(name)
        at = end
    pieces.append(text[at:])
    return "".join(pieces), settings
