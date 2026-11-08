"""The silent-differences catalogue (spec sections 6 and 17; ADR 0019): the one interface
through which the engine reads it.

The catalogue is split into a half per source and a half per target, which meet at the
operation vocabulary and share entry IDs. Each source provides its source half and each
target its target half, as data in its own package; the core defines their shape and reads
them only through what the engine hands it, by entry ID and by vocabulary operation. Each
half's version is the hash of its entries. Both versions appear in the report data's
versions, and are exposed for the test definition's fingerprint (independence rule 9's
"tolerance catalogue"), which reconciliation computes (issue #51).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping

from . import vocabulary


def _version(name: str, entries: list) -> str:
    """The hash of the half's content: whose half it is, and its entries."""
    canonical = json.dumps([name, entries], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class SourceEntry:
    """How one source construct behaves, in vocabulary terms."""
    id: str
    section: str
    example: str
    vocabulary: str                              # the Vocabulary cell, as the annex has it
    operations: tuple[str, ...]
    types: tuple[str, ...]
    arguments: Mapping[str, tuple[str, ...]]     # operation -> names that are its arguments
    behaviour: str
    evidence: str
    wording: str | None                          # the report's wording of the source's behaviour


@dataclasses.dataclass(frozen=True)
class TargetEntry:
    """Where the target's built-ins differ from the construct's meaning, and what reproduces it."""
    id: str
    section: str
    target_only: bool
    example: str | None                          # held only by a target-only entry
    behaviour: str
    behaviour_ansi_off: str | None
    reproduces: str
    evidence: str
    naive_built_in: str | None                   # what the reproducing helper replaces, for mutation
    wording: str | None                          # the report's wording of the target's behaviour


class _Half[E: (SourceEntry, TargetEntry)]:
    def __init__(self, entries: list[E], version: str):
        self._entries = {e.id: e for e in entries}
        if len(self._entries) != len(entries):
            raise ValueError("a differences half lists an entry ID twice")
        self.version = version

    def ids(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def entries(self) -> tuple[E, ...]:
        return tuple(self._entries.values())

    def entry(self, entry_id: str) -> E:
        try:
            return self._entries[entry_id]
        except KeyError:
            raise KeyError(f"silent difference {entry_id} is not in this half of the catalogue") from None

    def __contains__(self, entry_id: str) -> bool:
        return entry_id in self._entries


class SourceHalf(_Half[SourceEntry]):
    def __init__(self, source: str, entries: list[SourceEntry], version: str):
        super().__init__(entries, version)
        self.source = source

    @classmethod
    def from_data(cls, data: dict) -> "SourceHalf":
        entries = [SourceEntry(
            id=e["id"], section=e["section"], example=e["example"], vocabulary=e["vocabulary"],
            operations=tuple(e["operations"]), types=tuple(e["types"]),
            arguments={op: tuple(args) for op, args in e.get("arguments", {}).items()},
            behaviour=e["behaviour"], evidence=e["evidence"], wording=e.get("wording"),
        ) for e in data["entries"]]
        return cls(data["source"], entries, _version(data["source"], data["entries"]))

    def for_operation(self, operation: str) -> tuple[SourceEntry, ...]:
        if operation not in vocabulary.operations():
            raise ValueError(f"{operation} is not an operation in the vocabulary")
        return tuple(e for e in self._entries.values() if operation in e.operations)

    def for_type(self, type_name: str) -> tuple[SourceEntry, ...]:
        if type_name not in vocabulary.types():
            raise ValueError(f"{type_name} is not a type in the vocabulary")
        return tuple(e for e in self._entries.values() if type_name in e.types)


class TargetHalf(_Half[TargetEntry]):
    def __init__(self, target: str, entries: list[TargetEntry], version: str):
        super().__init__(entries, version)
        self.target = target

    @classmethod
    def from_data(cls, data: dict) -> "TargetHalf":
        entries = [TargetEntry(
            id=e["id"], section=e["section"], target_only=e.get("target_only", False), example=e.get("example"),
            behaviour=e["behaviour"], behaviour_ansi_off=e.get("behaviour_ansi_off"), reproduces=e["reproduces"],
            evidence=e["evidence"], naive_built_in=e.get("naive_built_in"), wording=e.get("wording"),
        ) for e in data["entries"]]
        return cls(data["target"], entries, _version(data["target"], data["entries"]))


class Catalogue:
    """One source half and one target half, read together. The engine builds it from the
    halves the source and the target provide; the core and the target read it only here."""

    def __init__(self, source: SourceHalf, target: TargetHalf):
        missing = [i for i in source.ids() if i not in target]
        if missing:
            raise ValueError(f"the target half has no entry for silent differences {', '.join(missing)}")
        self.source, self.target = source, target

    def for_operation(self, operation: str) -> tuple[tuple[SourceEntry, TargetEntry], ...]:
        """Every entry whose meaning the operation carries, with both halves."""
        return tuple((s, self.target.entry(s.id)) for s in self.source.for_operation(operation))

    def for_type(self, type_name: str) -> tuple[tuple[SourceEntry, TargetEntry], ...]:
        """Every entry whose meaning rests on the type, with both halves."""
        return tuple((s, self.target.entry(s.id)) for s in self.source.for_type(type_name))

    def difference(self, entry_id: str) -> str:
        """The report's wording of one difference: the source half says how the source
        behaves, the target half how the target does."""
        source = self.source.entry(entry_id) if entry_id in self.source else None
        target = self.target.entry(entry_id)
        parts = [target.wording] if target.target_only else [source and source.wording, target.wording]
        if not all(parts):
            raise LookupError(f"silent difference {entry_id} has no report wording in both halves of the catalogue")
        return f"Silent difference {entry_id}: " + "; ".join(p for p in parts if p)

    def versions(self) -> dict[str, str]:
        return {"differences_source": self.source.version, "differences_target": self.target.version}
