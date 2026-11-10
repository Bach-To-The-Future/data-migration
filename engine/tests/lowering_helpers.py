"""Lowering of SQL text against the corpus snapshot, without a database object per test: the
text is added to the snapshot as a synthetic module, with the facts the extractor would read."""
from __future__ import annotations

from migrt.core.catalog import CatalogObject, CatalogReader, Fact, Provenance, Snapshot
from migrt.core.ir import Artefact
from migrt.sources.sqlserver import extractor, lowering, names, parser

P = Provenance("server", "test: a synthetic module")


def lower_text(snapshot: Snapshot, sql: str, kind: str = "view", database: str = "RetailSample",
               schema: str = "sales", name: str = "vTest", ansi_nulls: bool = True, parameters=(),
               returns=None) -> tuple[Artefact, CatalogReader]:
    identity = f"{database}.{schema}.{name}"
    facts = [Fact("skeleton", "source_text", sql, P), Fact("skeleton", "extractable", True, P),
             Fact("skeleton", "secrets", _secrets(sql), P),
             Fact(extractor.SOURCE, "ansi_nulls", ansi_nulls, P), Fact(extractor.SOURCE, "quoted_identifier", True, P),
             Fact(extractor.SOURCE, "schema_bound", False, P)]
    if kind != "view":
        facts.append(Fact("skeleton", "parameters", list(parameters), P))
    if returns is not None:
        facts.append(Fact("skeleton", "returns", returns, P))
    module = CatalogObject(identity, kind, tuple(facts))
    extended = Snapshot.of("test", [o for o in snapshot.objects if o.identity != identity] + [module])
    reader = CatalogReader(extended, names.resolve)
    parsed = parser.parse(identity, sql, reader.value(database, "compatibility_level", extractor.SOURCE), True)
    return lowering.lower_artefact(reader, identity, parsed), reader


def _secrets(sql: str) -> list:
    from migrt.sources.sqlserver import secrets
    return extractor._marked("source_text", secrets.in_text(sql))


def query(artefact: Artefact):
    return artefact.body.child("query")


def kinds(node) -> list[str]:
    return [n.kind for n in node.walk()]


def of_kind(artefact: Artefact, kind: str):
    return [n for n in artefact.nodes() if n.kind == kind]
