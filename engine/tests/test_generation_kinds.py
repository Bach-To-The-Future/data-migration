"""Which node kinds the PySpark target generates, declared once (issue #107).

The generator has a handler for each kind it generates, and says which (`generator.handled_kinds()`). Detection
before generation refuses every kind that has no handler, so "claimed as generated" and "has code" are one fact by
construction: adding a handler is the only edit a kind needs.

Six artefact bodies and `declare_parameter` are reached through the artefact and its parameters, not through a plan
step or an expression, so they have handlers of their own kind; so does `return`, the statement of a scalar function.
"""
import generation_ir as ir
from generation_ir import column, node
from migrt.core import vocabulary
from migrt.core.ir import Artefact
from migrt.targets.pyspark import generator

STRUCTURAL = {"view_body", "table_function_body", "scalar_function_body", "procedure_body", "trigger_body",
              "batch_script_body", "declare_parameter", "return"}


def every_vocabulary_kind_once() -> Artefact:
    """An artefact with one node of every kind the vocabulary has, so that detection can be asked about each."""
    kinds = sorted(vocabulary.operations() | vocabulary.node_kinds())
    artefact = Artefact(ir.VIEW, "view", "", node("view_body", "", *[node(kind, "child") for kind in kinds]))
    ir.position(artefact)
    return artefact


def test_detection_refuses_exactly_the_kinds_the_generator_has_no_handler_for():
    artefact = every_vocabulary_kind_once()
    kinds = {n.kind for n in artefact.nodes()}

    refused = {f.kind for f in generator.detect(artefact) if f.kind in kinds}

    assert refused == kinds - generator.handled_kinds()


def test_the_structural_kinds_have_handlers_too():
    assert STRUCTURAL <= generator.handled_kinds()


def test_every_handled_kind_is_one_the_vocabulary_knows():
    assert generator.handled_kinds() <= vocabulary.operations() | vocabulary.node_kinds()


def test_a_kind_has_one_handler_in_one_role():
    handlers = [generator.PLAN_STEPS, generator.EXPRESSIONS, generator.BODIES, generator.STATEMENTS,
                generator.PARAMETERS]
    kinds = [kind for h in handlers for kind in h.by_kind]

    assert len(kinds) == len(set(kinds))


def view_with_a_limit():
    limit = node("limit", "input", ir.read_orders())
    return ir.view(node("project", "", limit, column("output", "Status")))


def test_a_handler_registered_for_a_kind_is_all_it_takes_for_the_kind_to_be_generated(monkeypatch):
    before = ir.generate(view_with_a_limit())
    assert any("limit has no PySpark generation" in m.reason for m in before.file.manual_items), before.records
    assert "limit" not in generator.handled_kinds()

    monkeypatch.setitem(generator.PLAN_STEPS.by_kind, "limit", lambda g, step: g.plan(step.child("input")))
    after = ir.generate(view_with_a_limit())

    assert "limit" in generator.handled_kinds()
    assert after.file.manual_items == []
    assert after.layer1.passed, after.layer1.failures
