"""The divider (issue #86, spec sections 4 and 5): a long artefact is divided along the boundaries of its statements
and nested blocks, never by line count, and each part carries the scope it starts in. The checker passes on every part.

Expected values come from the spec: a part is a run of consecutive statements of one block, in source order; parts
together hold every statement of the artefact exactly once; a part's scope is the scope of its first statement; a
statement too large for the budget is a part of its own, flagged `oversize`, never rejected."""
import json

from migrt.core import checker, divider
from migrt.sources.sqlserver import lowering

from lowering_helpers import lower_text, parameter
from procedural_helpers import lowered, lowered_script, proc

NESTED = ("statement", "then", "else", "body", "handler")


def sets(count: int, start: int = 1) -> str:
    return "\n".join(f"  SET @n = @n + {i};" for i in range(start, start + count))


SETS = sets(20)                                  # 20 statements of about 18 characters each
BODY = f"DECLARE @n int = 0;\n{SETS}"
IF_BODY = f"DECLARE @n int = 0;\nIF @n = 0\nBEGIN\n{sets(10)}\nEND\nELSE\nBEGIN\n{sets(10, 11)}\nEND\nSET @n = 99;"


def part_statements(part):
    return list(part.body.children)


def ids(nodes) -> list[str]:
    return [n.id for n in nodes]


def leaves(node):
    """Every statement of an artefact that has no statement inside it, in source order."""
    out = []
    for c in node.children:
        if c.role in NESTED:
            out += leaves(c) if any(d.role in NESTED[1:] for d in c.children) else [c]
    return out


def statement_ids(node) -> list[str]:
    """Every statement below a node, at any depth, in source order."""
    out = []
    for c in node.children:
        if c.role in NESTED:
            out += [c.id] + statement_ids(c)
    return out


def assert_nothing_dropped_or_repeated(artefact, parts) -> None:
    """The parts hold every statement once, in source order; the statements missing from them are the blocks the
    divider opened, which the parts name in their paths."""
    held = [i for p in parts for s in p.body.children for i in [s.id] + statement_ids(s)]
    opened = {e["node"] for p in parts for e in p.body.attrs["path"] if e["kind"] != "batch"}
    everything = statement_ids(artefact.body) + [b.id for b in artefact.body.children_with("batch")]
    for batch in artefact.body.children_with("batch"):
        everything += statement_ids(batch)
    assert sorted(held + sorted(opened)) == sorted(set(everything) - {b.id for b in artefact.body.children_with("batch")})
    assert len(held) == len(set(held))
    starts = [p.body.children[0].position.offset for p in parts]
    ends = [p.body.children[-1].position.offset + p.body.children[-1].position.length for p in parts]
    assert all(a < b <= c for a, b, c in zip(starts, ends, starts[1:] + [10 ** 9]))   # in order, no part overlapping another


def check_all(parts, columns):
    for part in parts:
        assert checker.check(part, columns) == [], part.body.attrs["index"]


# ---------------------------------------------------------------- statement boundaries

def test_an_artefact_within_the_budget_is_one_part(corpus_snapshot):
    artefact = lowered(corpus_snapshot, BODY)

    [only] = divider.divide(artefact, budget=10_000)

    assert ids(part_statements(only)) == ids(artefact.body.children_with("statement"))
    assert (only.body.attrs["index"], only.body.attrs["count"]) == (0, 1)
    assert only.body.attrs["path"] == []
    assert only.body.attrs["oversize"] is False


def test_a_long_artefact_is_divided_between_statements_in_source_order(corpus_snapshot):
    artefact = lowered(corpus_snapshot, BODY)
    original = artefact.body.children_with("statement")

    parts = divider.divide(artefact, budget=120)

    assert len(parts) > 2
    assert [p.body.attrs["index"] for p in parts] == list(range(len(parts)))
    assert {p.body.attrs["count"] for p in parts} == {len(parts)}
    # together the parts hold every statement once, in order: nothing is dropped and nothing is repeated
    assert [s for p in parts for s in ids(part_statements(p))] == ids(original)
    for part in parts:
        size = part.body.attrs["size"]
        assert size <= 120 and part.body.attrs["oversize"] is False
        first, last = part_statements(part)[0].position, part_statements(part)[-1].position
        assert size == last.offset + last.length - first.offset      # the source text a part spans, not its line count


def test_each_part_carries_the_scope_it_starts_in(corpus_snapshot):
    artefact = lowered(corpus_snapshot, BODY)

    parts = divider.divide(artefact, budget=120)

    for part in parts:
        first = part_statements(part)[0]
        assert part.body.attrs["scope"] == first.attrs["scope"]
    assert parts[0].body.attrs["scope"]["variables"] == []
    assert all(p.body.attrs["scope"]["variables"] == ["@n"] for p in parts[1:])


def test_the_checker_passes_on_every_part(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, BODY)

    parts = divider.divide(artefact, budget=120)

    check_all(parts, lowering.columns_of(reader))


def test_dividing_twice_gives_the_same_documents(corpus_snapshot):
    artefact = lowered(corpus_snapshot, BODY)

    first = [p.document() for p in divider.divide(artefact, budget=120)]
    second = [p.document() for p in divider.divide(artefact, budget=120)]

    assert first == second
    assert all(json.loads(d)["body"]["kind"] == "part_body" for d in first)


# ---------------------------------------------------------------- nested blocks

def test_a_block_too_large_for_the_budget_is_opened_and_divided_inside(corpus_snapshot):
    artefact = lowered(corpus_snapshot, IF_BODY)
    [declare, branch, last] = artefact.body.children_with("statement")
    assert branch.kind == "if"

    parts = divider.divide(artefact, budget=150)

    inside = {role: [p for p in parts if p.body.attrs["path"] and p.body.attrs["path"][0]["role"] == role]
              for role in ("then", "else")}
    assert len(inside["then"]) >= 2 and len(inside["else"]) >= 2
    for role, found in inside.items():
        for part in found:
            [entry] = part.body.attrs["path"]
            assert (entry["node"], entry["kind"], entry["span"]) == (branch.id, "if", str(branch.span))
            assert entry["scope"] == branch.attrs["scope"]
            assert entry["condition"] == branch.child("condition").id
            assert {c.role for c in part.body.children} == {role}
    # the statements outside the block are parts of their own, around it, in source order
    assert [p.body.children[0].id for p in (parts[0], parts[-1])] == [declare.id, last.id]
    assert [s for p in parts for s in ids(part_statements(p))] == ids(leaves(artefact.body))


def test_the_scope_inside_an_opened_block_is_the_scope_of_that_branch(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, "DECLARE @n int = 0;\nIF @n = 0\nBEGIN\n  DECLARE @m int = 1;\n"
                            f"{sets(10)}\nEND\nELSE\nBEGIN\n{sets(10)}\nEND")

    parts = divider.divide(artefact, budget=150)

    then_parts = [p for p in parts if p.body.attrs["path"] and p.body.attrs["path"][0]["role"] == "then"]
    else_parts = [p for p in parts if p.body.attrs["path"] and p.body.attrs["path"][0]["role"] == "else"]
    assert all("@m" in p.body.attrs["scope"]["variables"] for p in then_parts[1:])
    assert all("@m" not in p.body.attrs["scope"]["variables"] for p in else_parts)    # the other branch's, until they join
    check_all(parts, lowering.columns_of(reader))


def test_every_statement_is_in_a_part_or_is_a_block_that_was_opened(corpus_snapshot):
    artefact = lowered(corpus_snapshot, IF_BODY)

    parts = divider.divide(artefact, budget=150)

    in_parts = {n.id for p in parts for n in p.body.walk() if n.kind != "part_body"}
    opened = {entry["node"] for p in parts for entry in p.body.attrs["path"]}
    opened_nodes = set(opened)         # a block opened stays where it is, with its condition and what it reads
    for block in (n for n in artefact.body.walk() if n.id in opened):
        for child in block.children:
            if child.role not in NESTED:
                opened_nodes |= {n.id for n in child.walk()}
    assert in_parts | opened_nodes == {n.id for n in artefact.nodes() if n.kind != "procedure_body"}
    assert not in_parts & opened_nodes


def test_a_loop_opened_by_the_divider_still_holds_its_break(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, "DECLARE @n int = 0;\nWHILE @n < 100\nBEGIN\n"
                            f"{sets(10)}\n  IF @n > 50 BREAK;\n{sets(10, 11)}\nEND")

    parts = divider.divide(artefact, budget=150)

    breaking = [p for p in parts if any(n.kind == "break" for n in p.body.walk())]
    assert breaking and all(p.body.attrs["path"][0]["kind"] == "while" for p in breaking)
    check_all(parts, lowering.columns_of(reader))
    # a part cut out of its loop is refused: its break has nothing to leave
    breaking[0].body.attrs["path"] = []
    assert any("outside every loop" in f.message for f in checker.check(breaking[0], lowering.columns_of(reader)))


def test_a_jump_may_leave_its_part_and_a_part_that_loses_the_labels_is_refused(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, f"DECLARE @n int = 0;\nIF @n = 0 GOTO done;\n{sets(10)}\ndone:\nSET @n = 2;")

    parts = divider.divide(artefact, budget=100)

    jumping = [p for p in parts if any(n.kind == "jump" for n in p.body.walk())]
    assert jumping and all(p.body.attrs["labels"] == ["done"] for p in parts)
    assert not any(n.kind == "declare_label" for p in jumping for n in p.body.walk())
    check_all(parts, lowering.columns_of(reader))
    jumping[0].body.attrs["labels"] = []
    assert any("jumps to the label" in f.message for f in checker.check(jumping[0], lowering.columns_of(reader)))


def test_a_part_names_the_declaration_of_everything_in_its_scope(corpus_snapshot):
    artefact, reader = proc(
        corpus_snapshot,
        "DECLARE @n int = 0;\nCREATE TABLE #t (a int);\nDECLARE @t TABLE (b int);\n"
        f"DECLARE c CURSOR LOCAL FOR SELECT o.OrderId, o.Status FROM sales.[Order] AS o;\n{sets(10)}",
        "@p varchar(5)", [parameter("@p", "varchar", length=5)])
    declared = {n.attrs["name"]: n for n in artefact.nodes() if n.kind.startswith(("declare_", "create_temp"))}

    parts = divider.divide(artefact, budget=100)

    last = parts[-1].body.attrs["declarations"]
    assert [(d["name"], d["group"], d["kind"], d["node"]) for d in last] == [
        ("@p", "parameters", "declare_parameter", declared["@p"].id),
        ("@n", "variables", "declare_variable", declared["@n"].id),
        ("@t", "row_sets", "declare_row_set_variable", declared["@t"].id),
        ("#t", "temp_tables", "create_temp_table", declared["#t"].id),
        ("c", "cursors", "declare_cursor", declared["c"].id)]
    assert [d["type"]["name"] for d in last[:2]] == ["string", "integer"]
    assert last[-1]["columns"] == 2
    check_all(parts, lowering.columns_of(reader))


def test_a_part_whose_start_state_is_wrong_is_refused(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, BODY)
    part = divider.divide(artefact, budget=120)[1]
    part.body.attrs["start_state"]["variables"] = []

    findings = checker.check(part, lowering.columns_of(reader))

    assert any("differs from the scope derived" in f.message for f in findings)
    assert any("not in scope" in f.message for f in findings)


def test_a_transaction_open_in_an_earlier_part_is_still_open_in_the_next(corpus_snapshot):
    """Deriving from the carried state gives what the whole artefact gives, including what a rollback would undo."""
    artefact, reader = proc(
        corpus_snapshot, f"DECLARE @n int = 0;\nBEGIN TRAN;\nCREATE TABLE #t (a int);\n{sets(10)}\nROLLBACK;\nSET @n = 3;")

    parts = divider.divide(artefact, budget=100)

    assert len(parts) > 2
    middle = parts[2].body.attrs["scope"]["transaction"]
    assert middle["min"] >= 1 and len(middle["begun"]) == 1
    assert parts[2].body.attrs["start_state"]["journal"] == [["created", "#t", 0]]
    check_all(parts, lowering.columns_of(reader))
    after = parts[-1].body.children[-1]                              # SET @n = 3, after the rollback
    assert after.kind == "assign" and after.attrs["scope"]["temp_tables"] == []   # the rollback undid the creation


def test_a_handler_is_divided_as_a_handler(corpus_snapshot):
    artefact, reader = proc(
        corpus_snapshot, f"DECLARE @n int = 0;\nBEGIN TRY\n{sets(10)}\nEND TRY\nBEGIN CATCH\n{sets(10, 11)}\nEND CATCH")

    parts = divider.divide(artefact, budget=150)

    assert {p.body.attrs["path"][0]["role"] for p in parts if p.body.attrs["path"]} == {"body", "handler"}
    assert all(p.body.attrs["path"][0]["kind"] == "try_catch" for p in parts if p.body.attrs["path"])
    check_all(parts, lowering.columns_of(reader))


# ---------------------------------------------------------------- scripts, large statements, queries

def test_parts_of_a_script_never_span_a_batch_and_variables_do_not_cross(corpus_snapshot):
    text = f"DECLARE @n int = 0;\n{SETS}\nGO\nDECLARE @n int = 5;\n{SETS}\nGO\nSELECT 1 AS one;"
    artefact = lowered_script(corpus_snapshot, text)
    batches = artefact.body.children_with("batch")

    parts = divider.divide(artefact, budget=150)

    for part in parts:
        [entry] = part.body.attrs["path"]
        assert entry["kind"] == "batch" and entry["node"] in {b.id for b in batches}
    order = [p.body.attrs["path"][0]["node"] for p in parts]
    assert order == sorted(order, key=[b.id for b in batches].index)
    assert set(order) == {b.id for b in batches}
    second = [p for p in parts if p.body.attrs["path"][0]["node"] == batches[1].id]
    assert second[0].body.attrs["scope"]["variables"] == []         # a new batch starts with no variables


def test_a_statement_larger_than_the_budget_is_a_part_of_its_own_and_flagged(corpus_snapshot):
    values = ", ".join(f"({i})" for i in range(60))
    artefact = lowered(corpus_snapshot, "DECLARE @n int = 0;\nSET @n = 1;\nCREATE TABLE #t (a int);\n"
                       f"INSERT INTO #t (a) VALUES {values};\nSET @n = 2;")

    parts = divider.divide(artefact, budget=120)

    [big] = [p for p in parts if p.body.attrs["oversize"]]
    assert [c.kind for c in big.body.children] == ["insert_rows"] and big.body.attrs["size"] > 120
    assert [s for p in parts for s in ids(part_statements(p))] == ids(artefact.body.children_with("statement"))


def test_a_query_with_no_statements_is_one_part(corpus_snapshot):
    artefact, reader = lower_text(corpus_snapshot, "CREATE VIEW sales.vTest AS SELECT o.OrderId FROM sales.[Order] AS o",
                                  kind="view")

    [only] = divider.divide(artefact, budget=10)

    assert only.body.attrs["count"] == 1 and [c.role for c in only.body.children] == ["query"]
    assert checker.check(only, lowering.columns_of(reader)) == []


def test_a_budget_that_is_not_a_number_of_characters_is_refused(corpus_snapshot):
    artefact = lowered(corpus_snapshot, BODY)

    try:
        divider.divide(artefact, budget=0)
    except ValueError as refused:
        assert "budget" in str(refused)
    else:
        raise AssertionError("a budget of zero was accepted")


def test_a_query_larger_than_the_budget_is_flagged_oversize(corpus_snapshot):
    artefact, _ = lower_text(corpus_snapshot, "CREATE VIEW sales.vTest AS SELECT o.OrderId FROM sales.[Order] AS o", kind="view")

    [only] = divider.divide(artefact, budget=10)

    assert only.body.attrs["oversize"] is True and only.body.attrs["size"] > 10


def test_a_part_whose_carried_scope_or_declarations_disagree_with_its_start_state_is_refused(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, BODY)
    parts = divider.divide(artefact, budget=120)
    scope_lie, declarations_lie = parts[1], parts[2]
    scope_lie.body.attrs["scope"] = dict(scope_lie.body.attrs["scope"], variables=[])
    declarations_lie.body.attrs["declarations"] = []

    assert any("carried scope" in f.message for f in checker.check(scope_lie, lowering.columns_of(reader)))
    assert any("declaration" in f.message for f in checker.check(declarations_lie, lowering.columns_of(reader)))
