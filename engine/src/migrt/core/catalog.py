"""The catalog's neutral shape and the one query interface conversion reads it through
(spec section 3).

Every fact carries its provenance: read from the server, parsed from a named file, or
assumed from a setting. Objects hold skeleton facts that core components read, and an
extension area named for the source that only that source's own code reads. A snapshot
is identified by a hash of its objects' content hashes, so re-extracting an unchanged
server gives the same snapshot and invalidates nothing.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any, Callable, Iterable

PROVENANCE_KINDS = ("server", "file", "assumed")
SKELETON = "skeleton"


@dataclasses.dataclass(frozen=True)
class Provenance:
    kind: str     # server, file or assumed
    detail: str   # the metadata view read, the file parsed, or the setting assumed

    def __post_init__(self):
        if self.kind not in PROVENANCE_KINDS:
            raise ValueError(f"provenance kind must be one of {PROVENANCE_KINDS}, not {self.kind!r}")
        if not self.detail:
            raise ValueError("provenance needs a detail: what was read, parsed or assumed")


@dataclasses.dataclass(frozen=True)
class Fact:
    area: str          # SKELETON, or the source's name for its extension area
    name: str
    value: Any         # JSON-serialisable
    provenance: Provenance


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


@dataclasses.dataclass(frozen=True)
class CatalogObject:
    identity: str      # as declared, fully qualified: database.schema.name
    kind: str          # server, database, table, view ...
    facts: tuple[Fact, ...]

    @property
    def content_hash(self) -> str:
        body = [self.identity, self.kind,
                sorted([f.area, f.name, f.value, f.provenance.kind, f.provenance.detail] for f in self.facts)]
        return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()

    def fact(self, name: str, area: str = SKELETON) -> Fact:
        for f in self.facts:
            if f.name == name and f.area == area:
                return f
        raise KeyError(f"{self.identity} has no {area} fact {name!r}")


def snapshot_id_for(objects: Iterable[CatalogObject]) -> str:
    digest = hashlib.sha256(_canonical(sorted((o.identity, o.content_hash) for o in objects)).encode("utf-8"))
    return "snap-" + digest.hexdigest()[:20]


@dataclasses.dataclass(frozen=True)
class Snapshot:
    id: str
    source: str
    objects: tuple[CatalogObject, ...]

    @staticmethod
    def of(source: str, objects: Iterable[CatalogObject]) -> "Snapshot":
        objects = tuple(sorted(objects, key=lambda o: o.identity))
        return Snapshot(snapshot_id_for(objects), source, objects)

    def get(self, identity: str) -> CatalogObject:
        for o in self.objects:
            if o.identity == identity:
                return o
        raise KeyError(f"{identity} is not in snapshot {self.id}")


@dataclasses.dataclass(frozen=True)
class CatalogRead:
    identity: str
    area: str
    fact: str
    content_hash: str


# A source's name resolver: given the snapshot and a name as written in the source,
# with the context it was written in, return the identity it denotes, or None.
NameResolver = Callable[[Snapshot, tuple[str, ...], dict], "str | None"]


class CatalogReader:
    """The query interface (spec section 3): conversion reads the catalog only through
    this, never storage, and every read is recorded so a conversion's cache key can be
    the hash of exactly the facts it read."""

    def __init__(self, snapshot: Snapshot, resolver: NameResolver):
        self.snapshot = snapshot
        self._resolver = resolver
        self.reads: list[CatalogRead] = []

    # Pseudo-facts recorded for reads that are not of a stored fact.
    IDENTITY, KIND = "(identity)", "(kind)"

    def _record(self, identity: str, area: str, name: str) -> None:
        read = CatalogRead(identity, area, name, self.snapshot.get(identity).content_hash)
        if read not in self.reads:
            self.reads.append(read)

    def resolve(self, parts: tuple[str, ...], context: dict) -> str | None:
        """Resolve a name as the source resolves it. The object it resolves to is a read:
        a name that resolves differently after a re-extraction changes the reads' hash."""
        found = self._resolver(self.snapshot, parts, context)
        if found is not None:
            self._record(found, SKELETON, self.IDENTITY)
        return found

    def fact(self, identity: str, name: str, area: str = SKELETON) -> Fact:
        found = self.snapshot.get(identity).fact(name, area)
        self._record(identity, area, name)
        return found

    def value(self, identity: str, name: str, area: str = SKELETON) -> Any:
        return self.fact(identity, name, area).value

    def kind_of(self, identity: str) -> str:
        self._record(identity, SKELETON, self.KIND)
        return self.snapshot.get(identity).kind

    def reads_hash(self) -> str:
        return hashlib.sha256(_canonical(sorted(dataclasses.astuple(r) for r in self.reads)).encode()).hexdigest()
