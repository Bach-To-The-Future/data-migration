"""An artefact that fails to parse (issue #86, spec section 4 item 5): it is divided at the boundaries a lexer can still
find, GO separators and statement terminators, and each part becomes an opaque fragment, so none is rejected for size.

Expected values come from the spec and T-SQL's lexical rules: a semicolon inside a string or a comment ends nothing; a
fragment is source text kept verbatim, with its position and its reason; with nothing parsed, nothing is known of what
a fragment reads or writes, so each one is a scope barrier, and what is therefore unknown is recorded."""
import pytest

from migrt.core import checker, divider
from migrt.sources.sqlserver import lowering, parser

from lowering_helpers import lower_source_text, lower_text, synthetic_module

BROKEN = ("CREATE PROCEDURE sales.uspTest AS BEGIN\n"
          "  DECLARE @n int = 0;\n"
          "  SET @n = @n + 1;\n"
          "  SELEKT 1 FROM;\n"
          "  SET @n = 2;\n"
          "END")


def fragments(artefact):
    return artefact.body.children_with("statement")


def lost(gap: str) -> list[str]:
    """What lies between two fragments other than whitespace and the GO lines that separate batches."""
    return [line for line in gap.splitlines() if line.strip() and not line.strip().upper().startswith("GO")]


def assert_verbatim(artefact, nodes) -> None:
    """Each fragment is the source text at its position, and only whitespace and GO lines lie between fragments."""
    text, at = artefact.original_source, 0
    for node in nodes:
        offset, length = node.position.offset, node.position.length
        assert node.attrs["text"] == text[offset:offset + length]
        assert lost(text[at:offset]) == [], f"text dropped before {node.position}"
        at = offset + length
    assert lost(text[at:]) == []


def test_text_that_does_not_parse_is_divided_at_statement_terminators_into_opaque_fragments(corpus_snapshot):
    artefact, reader = lower_source_text(corpus_snapshot, BROKEN, kind="procedure")

    found = fragments(artefact)

    assert artefact.body.kind == "procedure_body" and artefact.artefact_type == "procedure"
    assert [n.attrs["text"].split("\n")[-1].strip() for n in found] == [
        "DECLARE @n int = 0;", "SET @n = @n + 1;", "SELEKT 1 FROM;", "SET @n = 2;", "END"]
    assert all(n.kind == "opaque_fragment" and n.attrs["scope_barrier"] is True for n in found)
    assert_verbatim(artefact, found)
    assert checker.check(artefact, lowering.columns_of(reader)) == []


def test_each_fragment_says_why_and_the_one_holding_the_error_names_it(corpus_snapshot):
    artefact, _ = lower_source_text(corpus_snapshot, BROKEN, kind="procedure")

    found = fragments(artefact)

    assert all("did not parse" in n.attrs["reason"] and "lexer" in n.attrs["reason"] for n in found)
    holding = [n for n in found if "L4:" in n.attrs["reason"]]
    assert [n.attrs["text"] for n in holding] == ["SELEKT 1 FROM;"]
    assert all("L4:" not in n.attrs["reason"] for n in found if n not in holding)


def test_what_is_unknown_because_nothing_parsed_is_recorded(corpus_snapshot):
    artefact, _ = lower_source_text(corpus_snapshot, BROKEN, kind="procedure")

    assert any("did not parse" in a and "scope barrier" in a for a in artefact.assumptions)
    assert all(n.attrs["references"] == [] for n in fragments(artefact))


def test_a_semicolon_in_a_string_or_a_comment_ends_nothing_and_a_comment_on_the_same_line_stays_with_its_statement(corpus_snapshot):
    text = "CREATE PROCEDURE sales.uspTest AS\nPRINT 'a;b'; -- c;d\n/* e; f */ SELEKT FROM;\n"

    artefact, _ = lower_source_text(corpus_snapshot, text, kind="procedure")

    assert [n.attrs["text"] for n in fragments(artefact)] == [
        "CREATE PROCEDURE sales.uspTest AS\nPRINT 'a;b'; -- c;d", "/* e; f */ SELEKT FROM;"]


def test_a_comment_after_the_last_terminator_is_not_a_fragment_and_is_not_dropped(corpus_snapshot):
    text = "CREATE PROCEDURE sales.uspTest AS\nSELEKT FROM;\n-- the end"

    artefact, _ = lower_source_text(corpus_snapshot, text, kind="procedure")

    [only] = fragments(artefact)
    assert only.attrs["text"].endswith("-- the end")
    assert_verbatim(artefact, [only])


def test_a_script_is_divided_at_go_into_batches_each_divided_at_terminators(corpus_snapshot):
    text = "SELECT 1 AS one;\nSELECT 2 AS two;\nGO\nSELEKT FROM;\nGO\nSELECT 3 AS three"

    artefact, reader = lower_source_text(corpus_snapshot, text, kind="query_batch", name="broken")

    batches = artefact.body.children_with("batch")
    assert artefact.body.kind == "batch_script_body"
    assert [[n.attrs["text"] for n in b.children_with("statement")] for b in batches] == [
        ["SELECT 1 AS one;", "SELECT 2 AS two;"], ["SELEKT FROM;"], ["SELECT 3 AS three"]]
    assert_verbatim(artefact, [n for b in batches for n in b.children_with("statement")])
    assert checker.check(artefact, lowering.columns_of(reader)) == []


def test_a_go_with_a_count_is_kept_not_dropped(corpus_snapshot):
    text = "SELEKT FROM;\nGO 5\nSELECT 1 AS one"

    artefact, _ = lower_source_text(corpus_snapshot, text, kind="query_batch", name="broken")

    first, second = artefact.body.children_with("batch")
    assert first.attrs["separator"] == "GO 5" and "separator" not in second.attrs


def test_a_view_is_one_fragment_because_a_query_has_no_boundary_a_lexer_can_find(corpus_snapshot):
    text = "CREATE VIEW sales.vTest AS SELEKT o.OrderId FROM sales.[Order] AS o;"

    artefact, reader = lower_source_text(corpus_snapshot, text, kind="view")

    [query] = artefact.body.children_with("query")
    assert artefact.body.kind == "view_body" and query.kind == "opaque_fragment" and query.attrs["text"] == text
    assert checker.check(artefact, lowering.columns_of(reader)) == []


def test_a_marked_secret_is_masked_in_a_fragment_and_named_in_the_artefact(corpus_snapshot):
    text = "CREATE PROCEDURE sales.uspTest AS\nCREATE LOGIN x WITH PASSWORD = 'hunter2';\nSELEKT FROM;"

    artefact, _ = lower_source_text(corpus_snapshot, text, kind="procedure")

    assert "hunter2" not in "".join(n.attrs["text"] for n in fragments(artefact))
    [setting] = artefact.secret_settings
    assert setting in fragments(artefact)[0].attrs["text"]
    assert "hunter2" in artefact.original_source           # the client's own record, kept verbatim


def test_text_that_parses_is_lowered_as_before(corpus_snapshot):
    text = "CREATE PROCEDURE sales.uspTest AS BEGIN\nDECLARE @n int = 0;\nSET @n = 1;\nEND"
    reader, identity = synthetic_module(corpus_snapshot, text, "procedure", name="uspTest")
    plain, _ = lower_text(corpus_snapshot, text, "procedure", name="uspTest")

    assert lowering.lower_source(reader, identity).document() == plain.document()


def test_a_long_broken_procedure_is_not_rejected_and_divides_by_the_blocks_of_fragments(corpus_snapshot):
    body = "\n".join(f"  SET @n = @n + {i};" for i in range(200))
    text = f"CREATE PROCEDURE sales.uspTest AS BEGIN\nDECLARE @n int = 0;\n{body}\nSELEKT FROM;\nEND"
    artefact, reader = lower_source_text(corpus_snapshot, text, kind="procedure")

    parts = divider.divide(artefact, budget=500)

    assert len(parts) > 5 and not any(p.body.attrs["oversize"] for p in parts)
    for part in parts:
        assert checker.check(part, lowering.columns_of(reader)) == []
    assert [s.id for p in parts for s in p.body.children] == [n.id for n in fragments(artefact)]


def test_a_text_that_does_not_parse_still_fails_to_parse_for_the_parser_itself(corpus_snapshot):
    """The lexer's division is a separate path: parsing is unchanged and still refuses."""
    reader, identity = synthetic_module(corpus_snapshot, BROKEN, "procedure", name="uspTest")

    with pytest.raises(parser.ParseFailed):
        parser.parse(identity, BROKEN, 150, True)


# ---------------------------------------------------------------- what generation does with them

BROKEN_BY_KIND = {
    "view": "CREATE VIEW sales.vTest AS SELEKT o.OrderId FROM sales.[Order] AS o;",
    "inline_function": "CREATE FUNCTION sales.vTest (@n int) RETURNS TABLE AS RETURN SELEKT 1 AS one;",
    "scalar_function": "CREATE FUNCTION sales.vTest (@n int) RETURNS int AS BEGIN\nSELEKT 1;\nRETURN 1;\nEND",
    "table_function": "CREATE FUNCTION sales.vTest () RETURNS @t TABLE (a int) AS BEGIN\nSELEKT 1;\nRETURN;\nEND",
    "procedure": BROKEN,
    "trigger": "CREATE TRIGGER sales.vTest ON sales.[Order] AFTER UPDATE AS BEGIN\nSELEKT 1;\nEND",
    "query_batch": "SELECT 1 AS one;\nGO\nSELEKT FROM;",
}


@pytest.mark.parametrize("kind", sorted(BROKEN_BY_KIND))
def test_generation_gives_every_fragment_a_manual_item_and_never_drops_one(corpus_snapshot, kind):
    from generation_helpers import names_for
    from migrt.core.differences import Catalogue
    from migrt.sources.sqlserver import differences as source_differences
    from migrt.targets.pyspark import differences as target_differences, generator

    artefact, reader = lower_source_text(corpus_snapshot, BROKEN_BY_KIND[kind], kind=kind)
    assert checker.check(artefact, lowering.columns_of(reader)) == []
    fragments_in = [n for n in artefact.nodes() if n.kind == "opaque_fragment"]
    assert fragments_in

    names = names_for(corpus_snapshot, {artefact.identity: kind})
    file = generator.generate(artefact, "snapshot-test", "0.0.0", Catalogue(source_differences.half(), target_differences.half()),
                              names, generator.Dependencies(kinds={}, generated=set()))

    assert {f.id for f in fragments_in} <= {m.node for m in file.manual_items} | {m.replaces for m in file.manual_items}
    assert generator.check(artefact, file).passed


# ---------------------------------------------------------------- review follow-ups

def test_a_parser_message_that_quotes_a_marked_secret_is_masked(corpus_snapshot):
    text = "CREATE PROCEDURE sales.uspTest AS\nCREATE LOGIN x WITH PASSWORD = 'hunter2' 'hunter2';\nSELECT 1;"

    artefact, _ = lower_source_text(corpus_snapshot, text, kind="procedure")

    shown = " ".join(str(n.attrs["reason"]) for n in artefact.nodes() if n.kind == "opaque_fragment")
    shown += " ".join(artefact.assumptions)
    assert "L2:" in shown and "hunter2" not in shown      # the parser reported the error, and its quotation is masked


def test_text_with_characters_outside_the_basic_plane_is_divided_where_the_text_says(corpus_snapshot):
    text = "CREATE PROCEDURE sales.uspTest AS\nPRINT N'😀 😀'; -- 😀😀\nSELEKT FROM;\nSELECT 1 AS one;"

    artefact, _ = lower_source_text(corpus_snapshot, text, kind="procedure")

    found = fragments(artefact)
    assert [n.attrs["text"].split("\n")[-1] for n in found] == ["PRINT N'😀 😀'; -- 😀😀", "SELEKT FROM;", "SELECT 1 AS one;"]
    assert_verbatim(artefact, found)


def test_a_comment_after_go_is_not_part_of_the_separator(corpus_snapshot):
    text = "SELEKT FROM;\nGO 3 -- three times\nSELECT 1 AS one"

    artefact, _ = lower_source_text(corpus_snapshot, text, kind="query_batch", name="broken")

    assert artefact.body.children_with("batch")[0].attrs["separator"] == "GO 3"
