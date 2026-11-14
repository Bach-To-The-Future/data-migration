"""The checker over the whole corpus (issue #85): every procedure, trigger, function and batch script of the three corpus
databases and the sample's script files lowers, and the checker passes on it or reports what it found. A construct lowering
does not express is an opaque fragment with its reason; this test names the ones that are cursors, transactions, errors,
messages, row counts and identity, which are lowered, so one left opaque has a reason that is about its own shape."""
import collections
import pathlib

from migrt.core import checker
from migrt.sources.sqlserver import extractor, lowering, parser
from migrt.core.catalog import CatalogReader
from migrt.sources.sqlserver import names

from lowering_helpers import lower_text

KINDS = ("procedure", "trigger", "scalar_function", "inline_function", "table_function")
BATCHES = pathlib.Path(__file__).parents[2] / "corpus" / "sample" / "artefacts" / "batches"
# the words that start a statement this ticket lowers
LOWERED_STATEMENTS = ("BEGIN TRY", "BEGIN TRAN", "COMMIT", "ROLLBACK", "SAVE TRAN", "PRINT", "RAISERROR", "THROW",
                      "OPEN ", "CLOSE ", "FETCH ", "DEALLOCATE ", "SET TRANSACTION", "SET IDENTITY_INSERT")


def lowered_modules(snapshot):
    reader = CatalogReader(snapshot, names.resolve)
    for o in sorted((o for o in snapshot.objects if o.kind in KINDS), key=lambda o: o.identity):
        if not reader.value(o.identity, "extractable"):
            continue
        database = o.identity.split(".", 1)[0]
        parsed = parser.parse(o.identity, reader.value(o.identity, "source_text"),
                              reader.value(database, "compatibility_level", extractor.SOURCE),
                              reader.value(o.identity, "quoted_identifier", extractor.SOURCE))
        yield o.identity, lowering.lower_artefact(reader, o.identity, parsed), reader


def test_the_checker_passes_on_every_corpus_procedure_trigger_and_function(corpus_snapshot):
    failures, count = {}, 0
    for identity, artefact, reader in lowered_modules(corpus_snapshot):
        count += 1
        found = checker.check(artefact, lowering.columns_of(reader))
        if found:
            failures[identity] = [f"{f.node_id}: {f.message}" for f in found]
    assert count >= 50, "the corpus databases were not extracted"
    assert failures == {}


def test_the_checker_passes_on_every_sample_batch_script(corpus_snapshot):
    failures = {}
    for path in sorted(BATCHES.glob("*.sql")):
        artefact, reader = lower_text(corpus_snapshot, path.read_text(encoding="utf-8"), kind="query_batch", name=path.stem)
        found = checker.check(artefact, lowering.columns_of(reader))
        if found:
            failures[path.name] = [f"{f.node_id}: {f.message}" for f in found]
    assert failures == {}


def test_no_statement_the_ticket_lowers_is_left_opaque_in_the_corpus(corpus_snapshot):
    left = collections.defaultdict(list)
    for identity, artefact, _ in lowered_modules(corpus_snapshot):
        for n in artefact.nodes():
            if n.kind == "opaque_fragment" and n.role in ("statement", "then", "else", "body", "handler") \
                    and n.attrs["text"].lstrip().upper().startswith(LOWERED_STATEMENTS):
                left[n.attrs["reason"]].append(f"{identity} {n.span}: {n.attrs['text'][:50]!r}")
    assert dict(left) == {}, "\n".join(f"{reason}: {where[:3]} ({len(where)})" for reason, where in left.items())
