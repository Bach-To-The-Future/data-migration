"""The generator's draft: everything generation has put into the file so far, and giving up a construct.

Giving up undoes the attempt completely: whatever the attempt wrote (lines, imports, names, settings, caveats,
assumptions, a failing function) is gone, and what is left is one manual item and whatever the caller puts in its
place. These tests do not know which fields a draft has: they change every one of them, so a field added later is
covered, or the test fails (issue #106).
"""
import copy

import pytest

from migrt.core.ir import Node
from migrt.targets.pyspark.draft import Draft, GivenUp, Unsupported


def construct(kind: str = "money_multiply") -> Node:
    node = Node(kind, None)
    node.id = "n7"
    return node


def disturb(draft: Draft) -> None:
    """Change every field of the draft, whatever its type."""
    for name, value in vars(draft).items():
        if isinstance(value, bool):
            setattr(draft, name, not value)
        elif isinstance(value, list):
            value.append("disturbed" if not value or isinstance(value[0], str) else value[0])
        elif isinstance(value, dict):
            value["disturbed"] = ["x"] if any(isinstance(v, list) for v in value.values()) else "x"
        elif isinstance(value, set):
            value.add("disturbed")
        else:
            raise AssertionError(f"the draft has a field {name} of type {type(value).__name__}: teach this test to change it")


def fields_but_the_manual_items(draft: Draft) -> dict:
    return {name: copy.deepcopy(value) for name, value in vars(draft).items() if name != "manual"}


def used(draft: Draft) -> Draft:
    draft.emit("def f(ss):", [construct("view_body")])
    draft.imports["g"] = "from generated.x import g"
    draft.taken.add("g")
    draft.settings_used.append("a.secret_1")
    draft.assumptions.append("an assumption made before")
    return draft


def test_giving_up_restores_every_field_and_leaves_one_manual_item():
    draft = used(Draft())
    before = fields_but_the_manual_items(draft)

    def attempt():
        disturb(draft)
        raise Unsupported(construct(), "no money type yet")

    outcome = draft.attempt(attempt)

    assert isinstance(outcome, GivenUp)
    assert fields_but_the_manual_items(draft) == before
    assert draft.manual == [outcome.item]
    assert outcome.item.id == "M1" and "no money type yet" in outcome.item.reason


def test_an_attempt_that_succeeds_returns_its_value_and_keeps_what_it_wrote():
    draft = Draft()

    outcome = draft.attempt(lambda: draft.emit("x = 1") or "x")

    assert outcome == "x" and draft.lines == ["x = 1"] and draft.manual == []


def test_an_inner_give_up_is_kept_by_a_successful_outer_attempt_and_discarded_by_a_failed_one():
    draft = Draft()

    def inner_gives_up():
        assert isinstance(draft.attempt(lambda: _fail("inner"), output=construct()), GivenUp)
        return "outer finished"

    assert draft.attempt(inner_gives_up) == "outer finished"
    assert [m.id for m in draft.manual] == ["M1"] and "inner" in draft.manual[0].reason

    def inner_then_outer_gives_up():
        draft.attempt(lambda: _fail("inner again"))
        _fail("outer")

    outcome = draft.attempt(inner_then_outer_gives_up)
    assert isinstance(outcome, GivenUp)
    assert [m.id for m in draft.manual] == ["M1", "M2"] and "outer" in draft.manual[1].reason


def test_a_give_up_names_the_output_column_it_fails_in_place_of_and_the_subtree_it_replaces():
    draft = Draft()
    output, replaced = construct("column"), construct("query")
    output.id, replaced.id = "n3", "n2"

    one = draft.attempt(lambda: _fail("a"), output=output)
    two = draft.attempt(lambda: _fail("b"), replaces=replaced)

    assert isinstance(one, GivenUp) and isinstance(two, GivenUp)
    assert (one.item.output, one.item.replaces) == ("n3", "n3")
    assert (two.item.output, two.item.replaces) == (None, "n2")


def test_a_reason_with_braces_in_it_is_kept_as_written():
    draft = Draft()

    outcome = draft.attempt(lambda: _fail("a {n} and a {0} in the reason"))

    assert isinstance(outcome, GivenUp)
    assert outcome.item.reason.startswith("Manual work 1: a {n} and a {0} in the reason")


def test_anything_but_unsupported_is_not_swallowed():
    draft = Draft()

    with pytest.raises(KeyError):
        draft.attempt(lambda: {}["missing"])


def _fail(reason: str, waits_for: tuple[str, ...] = ()):
    raise Unsupported(construct(), reason, waits_for)


def test_a_give_up_carries_the_tickets_the_construct_waits_for_and_the_reason_stays_plain():
    draft = Draft()

    one = draft.attempt(lambda: _fail("it needs the money type, which is not built yet", waits_for=("#87",)))
    two = draft.attempt(lambda: _fail("it needs a thing that is not built yet"))

    assert isinstance(one, GivenUp) and isinstance(two, GivenUp)
    assert one.item.waits_for == ("#87",) and two.item.waits_for == ()
    assert "#87" not in one.item.reason


def test_an_item_made_for_a_construct_never_attempted_can_wait_too():
    draft = Draft()

    item = draft.manual_item_at(construct("if"), "procedural code", waits_for=("#50",))

    assert item.waits_for == ("#50",) and "#50" not in item.reason
