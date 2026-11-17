"""`tools/corpus_blockers.py`: how far the corpus procedures and triggers are from generating whole, as numbers every ticket
report quotes before and after.

The logic is checked on small inputs; the corpus numbers are pinned here, so a ticket that changes them changes this file in
the same pull request, and the change is the ticket's "before and after". Whole procedures can never exceed the ceiling:
a procedure with dynamic SQL or a schema change stays manual work for good."""
import importlib.util
import pathlib
import sys

import generation_ir as ir
from generation_ir import node

TOOL = pathlib.Path(__file__).resolve().parents[2] / "tools" / "corpus_blockers.py"
spec = importlib.util.spec_from_file_location("corpus_blockers", TOOL)
assert spec is not None and spec.loader is not None
tool = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tool          # a dataclass looks its module up while it is made
spec.loader.exec_module(tool)

# What the corpus gives today (see docs/corpus/blockers-baseline.md). A ticket that moves any of these updates them.
PINNED = {"procedures": 82, "root_blockers": 544, "at_most_1": 13, "at_most_3": 25, "whole": 0, "ceiling": 65}


# ---------------------------------------------------------------- the arithmetic

def measurement(blockers: dict, tickets: dict):
    return tool.Measurement({p: frozenset(k) for p, k in blockers.items()}, tickets)


def test_the_summary_counts_blockers_procedures_with_few_left_whole_ones_and_the_ceiling():
    m = measurement({"a": {"x"}, "b": {"x", "y"}, "c": set(), "d": {"dynamic_sql_execution"}, "e": {"x", "y", "z", "w"}},
                    {"x": ("#1",), "y": ("#2",), "z": ("#2",), "w": ("none",)})

    s = tool.summarise(m)

    assert (s.procedures, s.root_blockers, s.at_most_1, s.at_most_3, s.whole) == (5, 8, 3, 4, 1)
    assert s.ceiling == 4                                  # d holds dynamic SQL, which is manual work for good


def test_a_ticket_is_the_only_remaining_one_for_a_procedure_whose_blockers_all_wait_for_it():
    m = measurement({"a": {"x"}, "b": {"x", "y"}, "c": {"y", "z"}},
                    {"x": ("#1",), "y": ("#2",), "z": ("#2",)})

    rows = {row.ticket: row for row in tool.summarise(m).by_ticket}

    assert (rows["#1"].procedures, rows["#1"].only) == (2, 1)       # a waits only for #1; b waits for #1 and #2
    assert (rows["#2"].procedures, rows["#2"].only) == (2, 1)       # c waits only for #2, with two kinds


def test_a_kind_that_waits_for_two_tickets_counts_for_each_and_is_never_the_only_one_for_either():
    m = measurement({"a": {"x"}}, {"x": ("#1", "#2")})

    rows = {row.ticket: row for row in tool.summarise(m).by_ticket}

    assert (rows["#1"].only, rows["#2"].only) == (0, 0)


def test_the_report_gives_the_headline_numbers_the_caveat_and_the_tables():
    m = measurement({"a": {"x"}}, {"x": ("#1",)})

    text = tool.report(m, full=True)

    assert "root blockers remaining: 1" in text and "at most 1 left: 1" in text and "whole: 0 of 1" in text
    assert "ceiling 1" in text and "lower bound" in text
    assert "| #1 |" in text and "| x |" in text and "| a |" in text


def test_a_comparison_gives_each_headline_number_before_and_after():
    before = measurement({"a": {"x", "y"}}, {"x": ("#1",), "y": ("#1",)})
    after = measurement({"a": {"x"}}, {"x": ("#1",)})

    text = tool.compare(before, after)

    assert "root blockers remaining: 2 -> 1" in text and "at most 1 left: 0 -> 1" in text and "whole: 0 -> 0" in text


# ---------------------------------------------------------------- what counts as a blocker

def test_a_node_refused_only_because_a_barrier_made_its_scope_uncertain_is_not_a_blocker_of_its_own():
    """The barrier is the root: a statement the generator handles is manual work only until the barrier is removed."""
    stale = node("assign", "statement", variable="@x", scope_may_be_stale="L1:1-L1:5")
    barrier_user = node("jump", "statement", label="done")
    result = ir.generate(ir.routine("procedure", stale, barrier_user))

    found = tool.blockers_of(result.artefact, result.file)

    assert found == {"jump"}


def test_a_kind_without_generation_that_sits_under_a_failed_statement_is_a_blocker_too():
    """A statement reports only its first failure; the unhandled kinds below it are blockers that wait their turn."""
    outer = node("if", "statement", node("exists", "condition"), node("declare_cursor", "then", name="@c"))
    result = ir.generate(ir.routine("procedure", outer))

    found = tool.blockers_of(result.artefact, result.file)

    assert {"exists", "declare_cursor"} <= found            # the condition fails first and hides the cursor beneath


# ---------------------------------------------------------------- the corpus

def test_the_corpus_numbers_are_the_ones_the_tickets_report_against(corpus_snapshot):
    s = tool.summarise(tool.measure(corpus_snapshot))

    assert {"procedures": s.procedures, "root_blockers": s.root_blockers, "at_most_1": s.at_most_1,
            "at_most_3": s.at_most_3, "whole": s.whole, "ceiling": s.ceiling} == PINNED
