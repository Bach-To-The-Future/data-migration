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

    def has_fact(self, name: str, area: str = SKELETON) -> bool:
        return any(f.name == name and f.area == area for f in self.facts)

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
    _by_identity: dict = dataclasses.field(default_factory=dict, init=False, repr=False, compare=False)
    _memo: dict = dataclasses.field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self):
        self._by_identity.update((o.identity, o) for o in self.objects)
        if len(self._by_identity) != len(self.objects):
            raise ValueError("a snapshot holds each identity once")

    @staticmethod
    def of(source: str, objects: Iterable[CatalogObject]) -> "Snapshot":
        objects = tuple(sorted(objects, key=lambda o: o.identity))
        return Snapshot(snapshot_id_for(objects), source, objects)

    def get(self, identity: str) -> CatalogObject:
        try:
            return self._by_identity[identity]
        except KeyError:
            raise KeyError(f"{identity} is not in snapshot {self.id}") from None

    def has(self, identity: str) -> bool:
        return identity in self._by_identity

    def memo(self, key: Any, build: Callable[[], Any]) -> Any:
        """A value derived from this snapshot alone, such as a source's name index, built once."""
        if key not in self._memo:
            self._memo[key] = build()
        return self._memo[key]


def fact_hash(fact: Fact) -> str:
    return hashlib.sha256(_canonical([fact.area, fact.name, fact.value, fact.provenance.kind,
                                      fact.provenance.detail]).encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class CatalogRead:
    identity: str
    area: str
    fact: str
    content_hash: str     # of the one fact read, or of the identity and kind for a pseudo-fact


@dataclasses.dataclass(frozen=True)
class Resolution:
    """What a name denotes, and every (identity, fact) the source consulted to find it, such
    as a synonym's base object. `identity` is None when the name denotes nothing in the catalog."""
    identity: str | None
    consulted: tuple[tuple[str, str], ...] = ()


# A source's name resolver: given the snapshot and a name as written in the source, with
# the context it was written in, say what it denotes.
NameResolver = Callable[[Snapshot, tuple[str, ...], dict], Resolution]


class CatalogReader:
    """The query interface (spec section 3): conversion reads the catalog only through
    this, never storage, and every read is recorded with the hash of exactly the fact read,
    so a conversion's cache key changes only when a fact it used changes."""

    def __init__(self, snapshot: Snapshot, resolver: NameResolver):
        self.snapshot = snapshot
        self._resolver = resolver
        self.reads: list[CatalogRead] = []

    # Pseudo-facts recorded for reads that are not of a stored fact.
    IDENTITY, KIND = "(identity)", "(kind)"

    def _record(self, identity: str, area: str, name: str) -> None:
        obj = self.snapshot.get(identity)
        if name in (self.IDENTITY, self.KIND):
            digest = hashlib.sha256(_canonical([obj.identity, obj.kind]).encode("utf-8")).hexdigest()
        else:
            digest = fact_hash(obj.fact(name, area))
        read = CatalogRead(identity, area, name, digest)
        if read not in self.reads:
            self.reads.append(read)

    def resolve(self, parts: tuple[str, ...], context: dict) -> str | None:
        """Resolve a name as the source resolves it. The object it resolves to, and every fact
        consulted on the way, is a read: a name that resolves differently after a re-extraction
        changes the reads' hash."""
        found = self._resolver(self.snapshot, parts, context)
        for identity, name in found.consulted:
            self._record(identity, SKELETON, name)
        if found.identity is not None:
            self._record(found.identity, SKELETON, self.IDENTITY)
        return found.identity

    def fact(self, identity: str, name: str, area: str = SKELETON) -> Fact:
        found = self.snapshot.get(identity).fact(name, area)
        self._record(identity, area, name)
        return found

    def value(self, identity: str, name: str, area: str = SKELETON) -> Any:
        return self.fact(identity, name, area).value

    def has_fact(self, identity: str, name: str, area: str = SKELETON) -> bool:
        """Whether the object holds the fact. Asking is not a read; reading it is."""
        return self.snapshot.get(identity).has_fact(name, area)

    def kind_of(self, identity: str) -> str:
        self._record(identity, SKELETON, self.KIND)
        return self.snapshot.get(identity).kind

    def referrers(self, identity: str) -> list[str]:
        """What refers to an object, such as what calls a procedure: every object whose
        references resolve to it, or name it as the synonym they went through. Every object's
        references are read to answer."""
        found = []
        for obj in self.snapshot.objects:
            if self.has_fact(obj.identity, "references"):
                if any(identity in (r.get("resolved"), r.get("via")) for r in self.value(obj.identity, "references")):
                    found.append(obj.identity)
        return found

    def triggers_on(self, table: str) -> list[str]:
        """The triggers on a table. Every trigger's table is read to answer."""
        return [o.identity for o in self.snapshot.objects
                if o.kind == "trigger" and self.value(o.identity, "table") == table]

    def reads_hash(self) -> str:
        return hashlib.sha256(_canonical(sorted(dataclasses.astuple(r) for r in self.reads)).encode()).hexdigest()
