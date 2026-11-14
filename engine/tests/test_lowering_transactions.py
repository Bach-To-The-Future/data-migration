"""Lowering of transactions and savepoints (issue #85): `begin_transaction`, `commit_transaction`,
`rollback_transaction`, `save_point`, `rollback_to_save_point`, `set_isolation_level`, `@@TRANCOUNT` and `XACT_STATE()`,
and what they do to the scope.

Expected values come from T-SQL's behaviour, which `test_statement_effects_sqlserver.py` and the sandbox's SQL Server
decide: a rollback undoes the creation of temp tables made inside the transaction and brings back one dropped inside
it, never undoes a table variable's rows, and a rollback to a savepoint leaves the transaction open."""
from procedural_helpers import (checked, kinds, lowered, lowered_script, opaque_statements, scope_of, script,
                                statements, trigger)

from lowering_helpers import of_kind


def tx(node) -> dict:
    return scope_of(node)["transaction"]


def test_begin_commit_and_rollback_lower_to_their_operations_in_order(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "BEGIN TRANSACTION t1;\nUPDATE sales.[Order] SET Status = 2 WHERE OrderId = 1;\n"
                       "COMMIT TRANSACTION t1;\nBEGIN TRAN;\nROLLBACK;")

    begin, update, commit, begin2, rollback = statements(artefact.body)
    assert kinds([begin, update, commit, begin2, rollback]) == [
        "begin_transaction", "update_rows", "commit_transaction", "begin_transaction", "rollback_transaction"]
    assert begin.attrs["name"] == "t1" and commit.attrs["name"] == "t1"
    assert begin2.attrs["name"] is None and rollback.attrs["name"] is None
    assert not opaque_statements(artefact)


def test_a_script_starts_with_no_open_transaction_and_the_depth_follows_begin_and_commit(corpus_snapshot):
    artefact = lowered_script(corpus_snapshot,
                              "BEGIN TRAN;\nBEGIN TRAN;\nUPDATE sales.[Order] SET Status = 2;\nCOMMIT;\nCOMMIT;\n")

    [batch] = artefact.body.children_with("batch")
    begin, begin2, update, commit, commit2 = statements(batch)
    none = {"min": 0, "max": 0, "begun": [], "save_points": []}
    assert tx(begin) == none
    assert (tx(begin2)["min"], tx(begin2)["max"]) == (1, 1) and tx(begin2)["begun"] == [str(begin.span)]
    assert (tx(update)["min"], tx(update)["max"]) == (2, 2) and tx(update)["begun"] == [str(begin.span), str(begin2.span)]
    assert (tx(commit)["min"], tx(commit)["max"]) == (2, 2)
    assert (tx(commit2)["min"], tx(commit2)["max"]) == (1, 1)
    # the last commit closes the outermost level: no transaction is open after it
    assert not of_kind(artefact, "opaque_fragment")


def test_a_procedure_may_be_called_inside_its_callers_transaction_so_the_depth_above_zero_is_unbounded(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "BEGIN TRAN;\nCOMMIT;")

    begin, commit = statements(artefact.body)
    assert tx(begin) == {"min": 0, "max": None, "begun": [], "save_points": []}
    assert (tx(commit)["min"], tx(commit)["max"]) == (1, None)


def test_a_trigger_always_runs_inside_the_transaction_of_the_statement_that_fired_it(corpus_snapshot):
    artefact, reader = trigger(corpus_snapshot, "ROLLBACK TRANSACTION;")
    assert checked(artefact, reader) == []

    [rollback] = statements(artefact.body)
    assert rollback.kind == "rollback_transaction"
    assert tx(rollback)["min"] == 1 and tx(rollback)["max"] is None


def test_the_depth_and_the_transaction_after_a_branch_cover_both_paths(corpus_snapshot):
    artefact = lowered_script(corpus_snapshot,
                              "DECLARE @n int = 1;\nIF @n = 1 BEGIN TRAN;\nSELECT 1;\n")

    [batch] = artefact.body.children_with("batch")
    *_, after = statements(batch)
    assert (tx(after)["min"], tx(after)["max"]) == (0, 1)
    assert len(tx(after)["begun"]) == 1                      # a transaction begun on one path may be open


def test_a_call_may_end_or_leave_open_any_transaction_so_it_leaves_the_depth_unknown(corpus_snapshot):
    artefact = lowered_script(corpus_snapshot, "BEGIN TRAN;\nEXEC sales.uspCloseStaleOrders @AsOf = '2020-01-01';\nCOMMIT;\n")

    [batch] = artefact.body.children_with("batch")
    begin, call, commit = statements(batch)
    assert call.kind == "call_procedure"
    assert (tx(commit)["min"], tx(commit)["max"]) == (0, None)


def test_a_rollback_undoes_the_creation_of_temp_tables_made_inside_the_transaction(corpus_snapshot):
    artefact = lowered_script(corpus_snapshot,
                              "CREATE TABLE #before (Id int);\nBEGIN TRAN;\nCREATE TABLE #inside (Id int);\n"
                              "ROLLBACK;\nSELECT 1;\n")

    [batch] = artefact.body.children_with("batch")
    begin, create, rollback, after = statements(batch)[1:]
    assert scope_of(rollback)["temp_tables"] == ["#before", "#inside"]
    assert scope_of(after)["temp_tables"] == ["#before"]
    assert tx(after) == {"min": 0, "max": 0, "begun": [], "save_points": []}


def test_a_rollback_brings_back_a_temp_table_dropped_inside_the_transaction(corpus_snapshot):
    artefact = lowered_script(corpus_snapshot,
                              "CREATE TABLE #kept (Id int);\nBEGIN TRAN;\nDROP TABLE #kept;\nROLLBACK;\nSELECT 1;\n")

    [batch] = artefact.body.children_with("batch")
    drop, rollback, after = statements(batch)[2:]
    assert scope_of(rollback)["temp_tables"] == []
    assert scope_of(after)["temp_tables"] == ["#kept"]


def test_a_commit_of_the_outermost_level_makes_creations_and_drops_permanent(corpus_snapshot):
    artefact = lowered_script(corpus_snapshot,
                              "CREATE TABLE #gone (Id int);\nBEGIN TRAN;\nCREATE TABLE #made (Id int);\n"
                              "DROP TABLE #gone;\nCOMMIT;\nBEGIN TRAN;\nROLLBACK;\nSELECT 1;\n")

    [batch] = artefact.body.children_with("batch")
    assert scope_of(statements(batch)[-1])["temp_tables"] == ["#made"]


def test_a_nested_commit_does_not_make_anything_permanent(corpus_snapshot):
    artefact = lowered_script(corpus_snapshot,
                              "BEGIN TRAN;\nBEGIN TRAN;\nCREATE TABLE #t (Id int);\nCOMMIT;\nROLLBACK;\nSELECT 1;\n")

    [batch] = artefact.body.children_with("batch")
    assert scope_of(statements(batch)[-1])["temp_tables"] == []


def test_a_rollback_never_undoes_a_table_variable(corpus_snapshot):
    artefact = lowered_script(corpus_snapshot,
                              "DECLARE @r TABLE (Id int);\nBEGIN TRAN;\nINSERT INTO @r (Id) VALUES (1);\nROLLBACK;\n"
                              "SELECT r.Id FROM @r AS r;\n")

    [batch] = artefact.body.children_with("batch")
    assert scope_of(statements(batch)[-1])["row_sets"] == ["@r"]
    [read] = [n for n in artefact.nodes() if n.kind == "read_table" and n.attrs.get("row_set")]
    assert read.attrs["row_set"] == "@r"


def test_a_savepoint_is_named_and_a_rollback_to_it_leaves_the_transaction_open(corpus_snapshot):
    artefact = lowered_script(corpus_snapshot,
                              "BEGIN TRAN;\nCREATE TABLE #s1 (Id int);\nSAVE TRAN sp;\nCREATE TABLE #s2 (Id int);\n"
                              "ROLLBACK TRAN sp;\nSELECT 1;\n")

    [batch] = artefact.body.children_with("batch")
    begin, c1, save, c2, back, after = statements(batch)
    assert save.kind == "save_point" and save.attrs["name"] == "sp"
    assert back.kind == "rollback_to_save_point" and back.attrs["save_point"] == "sp"
    assert tx(back)["save_points"] == ["sp"]
    # the creation after the savepoint is undone, the one before it is not, and the transaction is still open
    assert scope_of(after)["temp_tables"] == ["#s1"]
    assert (tx(after)["min"], tx(after)["max"]) == (1, 1) and tx(after)["save_points"] == ["sp"]


def test_a_commit_or_a_rollback_of_the_whole_transaction_forgets_its_savepoints(corpus_snapshot):
    artefact = lowered_script(corpus_snapshot, "BEGIN TRAN;\nSAVE TRAN a;\nSAVE TRAN b;\nCOMMIT;\nSELECT 1;\n")

    [batch] = artefact.body.children_with("batch")
    *_, commit, after = statements(batch)
    assert tx(commit)["save_points"] == ["a", "b"]
    assert tx(after)["save_points"] == []


def test_rolling_back_to_a_name_that_is_neither_a_savepoint_nor_a_transaction_is_kept_as_written(corpus_snapshot):
    artefact, reader = script(corpus_snapshot, "BEGIN TRAN;\nROLLBACK TRAN nowhere;\n")
    assert checked(artefact, reader) == []

    [fragment] = opaque_statements(artefact)
    assert fragment.attrs["scope_barrier"] is True
    assert "nowhere" in fragment.attrs["reason"]


def test_rolling_back_the_named_outermost_transaction_is_a_rollback_of_all_of_it(corpus_snapshot):
    artefact = lowered_script(corpus_snapshot, "BEGIN TRAN outer1;\nBEGIN TRAN;\nROLLBACK TRAN outer1;\nSELECT 1;\n")

    [batch] = artefact.body.children_with("batch")
    *_, rollback, after = statements(batch)
    assert rollback.kind == "rollback_transaction" and rollback.attrs["name"] == "outer1"
    assert (tx(after)["min"], tx(after)["max"]) == (0, 0)


def test_the_transaction_depth_and_state_are_values_of_their_own(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "DECLARE @n int = @@TRANCOUNT;\nIF XACT_STATE() = -1 ROLLBACK;")

    [depth] = of_kind(artefact, "transaction_depth")
    [state] = of_kind(artefact, "transaction_state")
    assert str(depth.type) == "integer(bits=32, signed=True)"
    assert str(state.type) == "integer(bits=16, signed=True)"
    assert not opaque_statements(artefact)


def test_an_isolation_level_is_recorded(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;")

    [level] = statements(artefact.body)
    assert level.kind == "set_isolation_level" and level.attrs["level"] == "SERIALIZABLE"


def test_a_transaction_option_that_changes_what_it_does_is_kept_as_written_with_its_reason(corpus_snapshot):
    artefact, reader = script(corpus_snapshot, "BEGIN DISTRIBUTED TRAN;\nBEGIN TRAN t WITH MARK 'x';\n"
                                              "COMMIT TRAN WITH (DELAYED_DURABILITY = ON);\n")
    assert checked(artefact, reader) == []

    fragments = opaque_statements(artefact)
    assert len(fragments) == 3 and all(f.attrs["scope_barrier"] is True for f in fragments)
    assert {w for f in fragments for w in ("DISTRIBUTED", "MARK", "DELAYED") if w in f.attrs["reason"].upper()} \
        == {"DISTRIBUTED", "MARK", "DELAYED"}


def test_commit_and_rollback_depend_on_the_transaction_scope_and_carry_the_flag_after_a_barrier(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "BEGIN TRAN;\nEXEC('x');\nCOMMIT;")

    begin, dynamic, commit = statements(artefact.body)
    assert "scope_may_be_stale" not in begin.attrs
    assert commit.attrs["scope_may_be_stale"] == str(dynamic.span)


def test_the_checker_refuses_a_transaction_scope_that_differs_from_the_one_derived(corpus_snapshot):
    artefact, reader = script(corpus_snapshot, "BEGIN TRAN;\nSELECT 1;\n")
    assert checked(artefact, reader) == []

    [select] = of_kind(artefact, "emit_result_set")
    select.attrs["scope"]["transaction"]["min"] = 0
    assert any("scope" in f.message and "derived" in f.message for f in checked(artefact, reader))


def test_a_transaction_begun_in_a_loop_may_be_opened_once_for_every_pass_so_the_depth_is_unbounded(corpus_snapshot):
    artefact = lowered_script(corpus_snapshot,
                              "DECLARE @n int = 0;\nWHILE @n < 3\nBEGIN\n  BEGIN TRAN;\n  SET @n += 1;\nEND\nSELECT 1;\n")

    [batch] = artefact.body.children_with("batch")
    [loop] = [s for s in statements(batch) if s.kind == "while"]
    [begin, step] = loop.children_with("body")
    assert (tx(begin)["min"], tx(begin)["max"]) == (0, None)         # the second pass starts with one open
    assert (tx(statements(batch)[-1])["min"], tx(statements(batch)[-1])["max"]) == (0, None)
