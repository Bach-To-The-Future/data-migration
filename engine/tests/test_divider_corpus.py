"""The divider over the corpus (issue #86): the 2,413-line procedure is divided along its parse tree, each part carrying
its scope, and the checker passes on every part of every procedure, trigger, function and script of the corpus.

The 2,413-line procedure (WideWorldImporters, `DataLoadSimulation.Configuration_ApplyDataLoadSimulationProcedures`) has
25 statements at its top level; the largest are blocks that build one very long string and execute it. Expected values
come from that shape: the blocks are opened, the string assignments cannot be divided and are flagged, and nothing is
dropped."""
import pathlib

from migrt.core import checker, divider
from migrt.core.catalog import CatalogReader
from migrt.sources.sqlserver import extractor, lowering, names, parser

from lowering_helpers import lower_text
from test_divider import NESTED, assert_nothing_dropped_or_repeated
from test_lowering_corpus import BATCHES, lowered_modules

LONGEST = "WideWorldImporters.DataLoadSimulation.Configuration_ApplyDataLoadSimulationProcedures"
BUDGET = 8_000


def longest(snapshot):
    reader = CatalogReader(snapshot, names.resolve)
    parsed = parser.parse(LONGEST, reader.value(LONGEST, "source_text"),
                          reader.value("WideWorldImporters", "compatibility_level", extractor.SOURCE),
                          reader.value(LONGEST, "quoted_identifier", extractor.SOURCE))
    return lowering.lower_artefact(reader, LONGEST, parsed), reader


def test_the_2413_line_procedure_is_divided_along_its_parse_tree_with_each_part_carrying_its_scope(corpus_snapshot):
    artefact, reader = longest(corpus_snapshot)
    assert artefact.original_source.count("\n") > 2400

    parts = divider.divide(artefact, BUDGET)

    assert len(parts) > 10
    original = {n.id: n for n in artefact.nodes()}
    for part in parts:
        first = part.body.children[0]
        assert part.body.attrs["scope"] == first.attrs["scope"]
        assert part.body.attrs["start_state"]["variables"] == first.attrs["scope"]["variables"]
        for statement in part.body.children:
            assert statement.role in NESTED
            assert statement.to_json() == original[statement.id].to_json()      # whole statements, not altered
    assert_nothing_dropped_or_repeated(artefact, parts)
    # blocks too large were opened; what remains over the budget is a statement that has nothing inside it
    assert any(p.body.attrs["path"] for p in parts)
    for part in parts:
        if part.body.attrs["oversize"]:
            [only] = part.body.children
            assert only.position.length > BUDGET and not any(c.role in NESTED[1:] for c in only.children)
        else:
            assert part.body.attrs["size"] <= BUDGET


def test_the_checker_passes_on_every_part_of_the_long_procedure(corpus_snapshot):
    artefact, reader = longest(corpus_snapshot)

    for part in divider.divide(artefact, BUDGET):
        assert checker.check(part, lowering.columns_of(reader)) == []


def test_the_checker_passes_on_every_part_of_every_corpus_artefact(corpus_snapshot):
    failures, counted = {}, 0
    for identity, artefact, reader in lowered_modules(corpus_snapshot):
        parts = divider.divide(artefact, 600)
        counted += len(parts)
        found = [(p.body.attrs["index"], f.message) for p in parts for f in checker.check(p, lowering.columns_of(reader))]
        if found:
            failures[identity] = found[:3]
        if artefact.body.kind != "view_body" and any(c.role in NESTED for c in artefact.body.children):
            assert_nothing_dropped_or_repeated(artefact, parts)
    assert counted > 200
    assert failures == {}


def test_the_checker_passes_on_every_part_of_every_sample_script(corpus_snapshot):
    failures = {}
    for path in sorted(pathlib.Path(BATCHES).glob("*.sql")):
        artefact, reader = lower_text(corpus_snapshot, path.read_text(encoding="utf-8"), kind="query_batch", name=path.stem)
        for part in divider.divide(artefact, 300):
            found = checker.check(part, lowering.columns_of(reader))
            if found:
                failures.setdefault(path.name, []).append((part.body.attrs["index"], found[0].message))
    assert failures == {}
