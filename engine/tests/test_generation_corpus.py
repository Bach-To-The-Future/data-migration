"""Generation over the whole corpus (issue #50): every procedure and trigger of the three corpus databases and every
batch script of the sample generates and passes layer 1, or is reported with the reason of each statement that is not
generated. A function with manual work raises when called and holds none of its code; one without is generated whole."""
from generation_helpers import names_for
from lowering_helpers import lower_text
from migrt.core import checker
from migrt.core.differences import Catalogue
from migrt.sources.sqlserver import differences as source_differences, lowering, statement_effects
from migrt.targets.pyspark import differences as target_differences, generator
from test_lowering_corpus import BATCHES, lowered_modules

CATALOGUE = Catalogue(source_differences.half(), target_differences.half())


def generated(snapshot, artefact, kind: str):
    names = names_for(snapshot, {artefact.identity: kind})
    file = generator.generate(artefact, "snapshot-test", "0.0.0", CATALOGUE, names, generator.Dependencies(),
                              statement_effects.effect)
    return file, generator.check(artefact, file)


def test_every_corpus_procedure_and_trigger_generates_or_is_reported_with_its_reasons(corpus_snapshot):
    whole, partly, failures, count = 0, 0, {}, 0
    for identity, artefact, reader in lowered_modules(corpus_snapshot):
        if artefact.artefact_type not in ("procedure", "trigger"):
            continue
        count += 1
        file, layer1 = generated(corpus_snapshot, artefact, artefact.artefact_type)
        if not layer1.passed:
            failures[identity] = layer1.failures
        if file.manual_items:
            partly += 1
            assert file.fails_when_called, identity                     # nothing of it runs
            assert all(m.reason and m.source_position and m.waits_for is not None for m in file.manual_items), identity
            assert not any("#" in m.reason for m in file.manual_items), identity
        else:
            whole += 1
            assert not file.fails_when_called, identity
    assert count >= 50, "the corpus databases were not extracted"
    assert failures == {}
    assert whole + partly == count


def test_every_sample_batch_script_generates_or_is_reported_with_its_reasons(corpus_snapshot):
    failures, count = {}, 0
    for path in sorted(BATCHES.glob("*.sql")):
        count += 1
        artefact, reader = lower_text(corpus_snapshot, path.read_text(encoding="utf-8"), kind="query_batch", name=path.stem)
        assert checker.check(artefact, lowering.columns_of(reader)) == []
        file, layer1 = generated(corpus_snapshot, artefact, "query_batch")
        if not layer1.passed:
            failures[path.name] = layer1.failures
        assert file.manual_items or not file.fails_when_called, path.name
    assert count >= 1 and failures == {}
