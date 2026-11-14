"""Lowering of SQL text against the corpus snapshot, without a database object per test: the
text is added to the snapshot as a synthetic module, with the facts the extractor would read."""
from __future__ import annotations

from migrt.core.catalog import CatalogObject, CatalogReader, Fact, Provenance, Snapshot
from migrt.core.ir import Artefact
from migrt.sources.sqlserver import extractor, lowering, names, parser

P = Provenance("server", "test: a synthetic module")


def synthetic_module(snapshot: Snapshot, sql: str, kind: str = "view", database: str = "RetailSample",
                     schema: str = "sales", name: str = "vTest", ansi_nulls: bool = True, parameters=(),
                     returns=None, table: str = "RetailSample.sales.Order", timing: str = "AFTER",
                     events=("INSERT", "UPDATE", "DELETE")) -> tuple[CatalogReader, str]:
    """The text added to the snapshot as a module, with the facts the extractor would read: its reader and identity.
    `kind` is the catalog kind: view, the functions, procedure, trigger (on `table`, with its timing and events) or
    query_batch (a file, whose identity is `batch:<name>.sql`)."""
    identity = f"batch:{name}.sql" if kind == "query_batch" else f"{database}.{schema}.{name}"
    facts = [Fact("skeleton", "source_text", sql, P), Fact("skeleton", "extractable", True, P),
             Fact("skeleton", "secrets", _secrets(sql), P),
             Fact(extractor.SOURCE, "ansi_nulls", ansi_nulls, P), Fact(extractor.SOURCE, "quoted_identifier", True, P),
             Fact(extractor.SOURCE, "schema_bound", False, P)]
    if kind in ("scalar_function", "inline_function", "table_function", "procedure"):
        facts.append(Fact("skeleton", "parameters", list(parameters), P))
    if kind == "trigger":
        facts += [Fact("skeleton", "table", table, P), Fact("skeleton", "timing", timing, P),
                  Fact("skeleton", "events", list(events), P)]
    if kind == "query_batch":
        facts.append(Fact("skeleton", "database", database, P))
    if returns is not None:
        facts.append(Fact("skeleton", "returns", returns, P))
    module = CatalogObject(identity, kind, tuple(facts))
    extended = Snapshot.of("test", [o for o in snapshot.objects if o.identity != identity] + [module])
    return CatalogReader(extended, names.resolve), identity


def lower_text(snapshot: Snapshot, sql: str, kind: str = "view", database: str = "RetailSample",
               schema: str = "sales", name: str = "vTest", ansi_nulls: bool = True, parameters=(),
               returns=None, table: str = "RetailSample.sales.Order", timing: str = "AFTER",
               events=("INSERT", "UPDATE", "DELETE")) -> tuple[Artefact, CatalogReader]:
    """Lower the text of a module (see `synthetic_module` for its arguments)."""
    reader, identity = synthetic_module(snapshot, sql, kind, database, schema, name, ansi_nulls, parameters, returns,
                                        table, timing, events)
    parsed = parser.parse(identity, sql, reader.value(database, "compatibility_level", extractor.SOURCE), True)
    return lowering.lower_artefact(reader, identity, parsed), reader


def lower_source_text(snapshot: Snapshot, sql: str, kind: str = "view", database: str = "RetailSample",
                      schema: str = "sales", name: str = "vTest", parameters=()) -> tuple[Artefact, CatalogReader]:
    """Lower a module the way a run does: text that does not parse is divided at lexer boundaries."""
    reader, identity = synthetic_module(snapshot, sql, kind, database, schema, name, parameters=parameters)
    return lowering.lower_source(reader, identity), reader


def _secrets(sql: str) -> list:
    from migrt.sources.sqlserver import secrets
    return extractor._marked("source_text", secrets.in_text(sql))


def query(artefact: Artefact):
    return artefact.body.child("query")


def kinds(node) -> list[str]:
    return [n.kind for n in node.walk()]


def of_kind(artefact: Artefact, kind: str):
    return [n for n in artefact.nodes() if n.kind == kind]


def parameter(name: str, type_: str = "int", *, direction: str = "input", length: int = 0, readonly: bool = False,
              default=None) -> dict:
    """A procedure parameter fact as the extractor reads it (an int, or a varchar of `length`)."""
    shape = {"int": (4, 10, 0), "varchar": (length, 0, 0), "datetime": (8, 23, 3)}[type_]
    return {"name": name, "declared_type": type_, "system_type": type_, "max_length": shape[0], "precision": shape[1],
            "scale": shape[2], "direction": direction, "readonly": readonly, "nullable": True, "default": default}
