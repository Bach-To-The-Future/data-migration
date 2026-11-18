"""Procedures that call each other (issue #118, spec section 6): a generated file imports the module of each artefact it
calls, so two that call each other, directly or through others, would import each other, which Python refuses at run time.
Until generation imports a callee inside the calling function, every call that closes such a ring is manual work with its
reason, so no generated file fails on import.

The ring is found from the calls the representation holds; nothing here needs a server."""
import pytest

from generation_helpers import generate_text
from lowering_helpers import synthetic_module
from migrt import engine
from migrt.core import callgraph

A, B, C, D = "db.s.A", "db.s.B", "db.s.C", "db.s.D"


# ---------------------------------------------------------------- the ring, as arithmetic on a graph

@pytest.mark.parametrize("calls, closing", [
    ({A: {B}, B: {A}}, {A: {B}, B: {A}}),                                       # two that call each other
    ({A: {B}, B: {C}, C: {A}}, {A: {B}, B: {C}, C: {A}}),                       # a ring of three
    ({A: {B}, B: {C}}, {}),                                                     # a chain is no ring
    ({A: {B, C}, B: {C}}, {}),                                                  # a diamond is no ring
    ({A: {A}}, {A: {A}}),                                                       # a call to itself
    ({A: {B}, B: {A, C}, C: set()}, {A: {B}, B: {A}}),                          # a callee outside the ring is not closing
    ({A: {B}, B: {A}, C: {A}, D: {C}}, {A: {B}, B: {A}}),                      # callers of a ring are not in it
    ({A: {B}}, {}),                                                             # a callee that calls nothing
])
def test_a_call_closes_a_ring_when_the_callee_reaches_the_caller_again(calls, closing):
    assert {k: set(v) for k, v in callgraph.closing_calls(calls).items()} == closing


# ---------------------------------------------------------------- generation

TWO_PROCEDURES = {
    "uspPing": "CREATE PROCEDURE sales.uspPing @n int AS BEGIN\nIF @n > 0 BEGIN\nSET @n = @n - 1;\n"
               "EXEC sales.uspPong @n = @n;\nEND\nEND",
    "uspPong": "CREATE PROCEDURE sales.uspPong @n int AS BEGIN\nIF @n > 0 BEGIN\nSET @n = @n - 1;\n"
               "EXEC sales.uspPing @n = @n;\nEND\nEND"}
PING, PONG = "RetailSample.sales.uspPing", "RetailSample.sales.uspPong"


def project_of_two(corpus_snapshot):
    from lowering_helpers import parameter
    snapshot = corpus_snapshot
    for name, sql in TWO_PROCEDURES.items():
        snapshot = synthetic_module(snapshot, sql, kind="procedure", name=name, parameters=[parameter("@n")])[0].snapshot
    return snapshot


def test_the_project_finds_the_ring_of_two_procedures_that_call_each_other(corpus_snapshot):
    snapshot = project_of_two(corpus_snapshot)

    rings = engine.closing_calls(snapshot, {PING: "procedure", PONG: "procedure"})

    assert {k: set(v) for k, v in rings.items()} == {PING: {PONG}, PONG: {PING}}


def test_a_call_that_closes_a_ring_is_manual_work_with_its_reason_and_the_ticket_that_builds_it(corpus_snapshot):
    from lowering_helpers import parameter
    snapshot = project_of_two(corpus_snapshot)

    generated = generate_text(snapshot, TWO_PROCEDURES["uspPing"], kind="procedure", name="uspPing",
                              parameters=[parameter("@n")], generated_in_project={PONG: "procedure"}, closing={PONG})

    [item] = generated.file.manual_items
    assert "each other" in item.reason and PONG in item.reason and "#" not in item.reason
    assert item.waits_for == ("#160",) and generated.file.fails_when_called


def test_each_procedure_of_the_ring_generates_without_importing_the_other(corpus_snapshot):
    from lowering_helpers import parameter
    snapshot = project_of_two(corpus_snapshot)
    rings = engine.closing_calls(snapshot, {PING: "procedure", PONG: "procedure"})

    files = {name: generate_text(snapshot, sql, kind="procedure", name=name, parameters=[parameter("@n")],
                                 generated_in_project={other: "procedure"}, closing=rings[f"RetailSample.sales.{name}"])
             for (name, sql), other in zip(TWO_PROCEDURES.items(), [PONG, PING])}

    for name, generated in files.items():
        assert generated.layer1.passed, generated.layer1.failures
        assert not any(line.startswith(("from generated", "import generated")) for line in generated.text.splitlines()), name


def test_a_call_that_does_not_close_a_ring_is_still_an_import(corpus_snapshot):
    from lowering_helpers import parameter
    snapshot = project_of_two(corpus_snapshot)

    generated = generate_text(snapshot, TWO_PROCEDURES["uspPing"], kind="procedure", name="uspPing",
                              parameters=[parameter("@n")], generated_in_project={PONG: "procedure"})

    assert generated.file.manual_items == []
    assert any(line.startswith("from generated") and "uspPong" in line for line in generated.text.splitlines())
