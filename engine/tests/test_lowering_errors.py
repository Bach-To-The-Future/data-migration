"""Lowering of errors, messages, row counts and identity (issue #85): `try_catch`, `raise_error`, `rethrow`,
`emit_message`, `error_field`, `last_statement_error`, `rows_affected`, `last_identity`, and what a TRY block does to
the scope. Expected values are T-SQL's: the effect each statement has on `@@ROWCOUNT` and `@@ERROR` is in
`test_statement_effects_sqlserver.py`, run against the sandbox SQL Server."""
from procedural_helpers import (checked, kinds, lowered, lowered_script, opaque_statements, proc, scope_of,
                                statements)

from lowering_helpers import of_kind


def tx(node) -> dict:
    return scope_of(node)["transaction"]


# ---------------------------------------------------------------- TRY / CATCH

def test_try_catch_has_a_block_and_a_handler_of_statements(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @n int = 1;\nBEGIN TRY\n  SET @n = 2;\n  SET @n = 3;\nEND TRY\n"
                       "BEGIN CATCH\n  SET @n = 4;\nEND CATCH")

    [guarded] = [s for s in statements(artefact.body) if s.kind == "try_catch"]
    assert kinds(guarded.children_with("body")) == ["assign", "assign"]
    assert kinds(guarded.children_with("handler")) == ["assign"]
    assert not opaque_statements(artefact)


def test_try_catch_is_no_longer_a_scope_barrier(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @n int = 1;\nBEGIN TRY SET @n = 2; END TRY BEGIN CATCH SET @n = 3; END CATCH\nSET @n = 4;")

    *_, guarded, after = statements(artefact.body)
    assert guarded.kind == "try_catch" and scope_of(after)["barrier"] is None
    assert not [n for n in artefact.nodes() if "scope_may_be_stale" in n.attrs]


def test_the_handler_may_run_after_any_statement_of_the_block_so_it_sees_a_table_dropped_or_made_in_it(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "CREATE TABLE #old (Id int);\n"
                       "BEGIN TRY\n  DROP TABLE #old;\n  CREATE TABLE #new (Id int);\nEND TRY\n"
                       "BEGIN CATCH\n  SELECT 1;\nEND CATCH\nSELECT 2;")

    _, guarded, after = statements(artefact.body)
    [handled] = guarded.children_with("handler")
    # the error may have come before the DROP, between the two, or after both
    assert scope_of(handled)["temp_tables"] == ["#old", "#new"]
    assert scope_of(after)["temp_tables"] == ["#old", "#new"]


def test_a_transaction_begun_in_the_block_may_be_open_in_the_handler_and_after_it(corpus_snapshot):
    artefact = lowered_script(corpus_snapshot,
                              "BEGIN TRY\n  BEGIN TRAN;\n  UPDATE sales.[Order] SET Status = 2;\n  COMMIT;\nEND TRY\n"
                              "BEGIN CATCH\n  IF @@TRANCOUNT > 0 ROLLBACK;\nEND CATCH\nSELECT 1;\n")

    [batch] = artefact.body.children_with("batch")
    guarded, after = statements(batch)
    [handler] = guarded.children_with("handler")
    assert (tx(handler)["min"], tx(handler)["max"]) == (0, 1)
    assert [r.kind for r in of_kind(artefact, "rollback_transaction")] == ["rollback_transaction"]
    assert (tx(after)["min"], tx(after)["max"]) == (0, 1)


def test_a_variable_declared_in_the_block_is_in_scope_in_the_handler_and_after_it(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "BEGIN TRY DECLARE @inner int = 1; END TRY BEGIN CATCH SET @inner = 2; END CATCH\nSET @inner = 3;")

    guarded, after = statements(artefact.body)
    assert scope_of(after)["variables"] == ["@inner"]
    assert scope_of(guarded.child("handler"))["variables"] == ["@inner"]


def test_a_cursor_opened_in_the_block_may_be_open_in_the_handler(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "BEGIN TRY\n  DECLARE c CURSOR LOCAL FOR SELECT 1 AS x;\n  OPEN c;\n  CLOSE c;\nEND TRY\n"
                       "BEGIN CATCH\n  SELECT 1;\nEND CATCH")

    [guarded] = statements(artefact.body)
    handled = scope_of(guarded.child("handler"))
    assert handled["cursors"] == ["c"] and handled["open_cursors"] == ["c"]


def test_a_try_in_a_loop_or_a_branch_keeps_its_own_statements_scopes(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @n int = 0;\nWHILE @n < 2 BEGIN\n  BEGIN TRY SET @n += 1; END TRY BEGIN CATCH BREAK; END CATCH\nEND")

    [loop] = [s for s in statements(artefact.body) if s.kind == "while"]
    [guarded] = loop.children_with("body")
    assert guarded.kind == "try_catch" and kinds(guarded.children_with("handler")) == ["break"]


# ---------------------------------------------------------------- RAISERROR, THROW, PRINT

def test_raiserror_keeps_the_message_severity_state_and_arguments(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @n int = 3;\nRAISERROR('Order %d is stale', 16, 1, @n) WITH NOWAIT, SETERROR;")

    [raised] = of_kind(artefact, "raise_error")
    assert raised.attrs["form"] == "raiserror" and raised.attrs["options"] == ["NOWAIT", "SETERROR"]
    assert raised.child("message").attrs["value"] == "Order %d is stale"
    assert raised.child("severity").attrs["value"] == 16 and raised.child("state").attrs["value"] == 1
    [argument] = raised.children_with("argument")
    assert argument.kind == "variable" and argument.attrs["name"] == "@n"


def test_throw_has_a_number_a_message_and_a_state_and_a_bare_throw_rethrows(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "BEGIN TRY THROW 50001, 'nope', 2; END TRY BEGIN CATCH THROW; END CATCH")

    [thrown] = of_kind(artefact, "raise_error")
    assert thrown.attrs["form"] == "throw" and thrown.attrs["options"] == []
    assert [thrown.child(r).attrs["value"] for r in ("number", "message", "state")] == [50001, "nope", 2]
    [again] = of_kind(artefact, "rethrow")
    assert again.role == "handler"


def test_raiserror_with_a_message_number_is_kept_as_written_because_the_catalog_does_not_hold_messages(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, "RAISERROR(50001, 16, 1);")
    assert checked(artefact, reader) == []

    [fragment] = opaque_statements(artefact)
    assert "message" in fragment.attrs["reason"] and fragment.attrs["scope_barrier"] is False


def test_print_sends_a_message_and_converts_what_is_not_a_string(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "DECLARE @n int = 3;\nPRINT 'n is ';\nPRINT @n;")

    text, number = of_kind(artefact, "emit_message")
    assert text.child("argument").value_type.name == "string"
    assert number.child("argument").kind == "implicit_convert" and number.child("argument").value_type.name == "string"


# ---------------------------------------------------------------- what a handler reads

def test_the_fields_of_the_error_being_handled_are_values_with_their_types(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @m nvarchar(4000);\nBEGIN TRY SELECT 1 / 0; END TRY BEGIN CATCH\n"
                       "SELECT ERROR_NUMBER() AS n, ERROR_SEVERITY() AS sev, ERROR_STATE() AS st, ERROR_LINE() AS l;\n"
                       "SET @m = ERROR_MESSAGE();\nSELECT ERROR_PROCEDURE() AS p;\nEND CATCH")

    fields = {n.attrs["field"]: n for n in of_kind(artefact, "error_field")}
    assert sorted(fields) == ["line", "message", "number", "procedure", "severity", "state"]
    assert str(fields["number"].type) == "integer(bits=32, signed=True)"
    assert fields["message"].value_type.name == "string" and fields["message"].value_type.length() == 4000
    assert fields["procedure"].value_type.name == "string" and fields["procedure"].value_type.length() == 128


def test_the_error_functions_may_be_read_outside_a_handler_in_a_procedure_a_handler_calls(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "SELECT ERROR_NUMBER() AS n;")

    assert [n.attrs["field"] for n in of_kind(artefact, "error_field")] == ["number"]


def test_the_error_number_and_the_row_count_are_values_of_the_last_statement(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @e int, @r int;\nUPDATE sales.[Order] SET Status = 2 WHERE OrderId = 1;\n"
                       "SELECT @e = @@ERROR, @r = @@ROWCOUNT;\nIF @@ROWCOUNT = 0 PRINT 'none';")

    [error] = of_kind(artefact, "last_statement_error")
    rows = of_kind(artefact, "rows_affected")
    assert len(rows) == 2 and str(error.type) == str(rows[0].type) == "integer(bits=32, signed=True)"
    assert not opaque_statements(artefact)


def test_a_row_count_read_is_not_made_a_constant_whatever_the_statement_before_it_is(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @r int;\nDELETE FROM sales.OrderLine WHERE OrderId = 1;\nSET @r = @@ROWCOUNT;")

    [assign] = of_kind(artefact, "assign")
    assert assign.child("argument").kind == "rows_affected"


# ---------------------------------------------------------------- identity

def test_the_last_identity_value_says_which_scope_it_comes_from(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @a numeric(38, 0), @b numeric(38, 0), @c numeric(38, 0);\n"
                       "INSERT INTO sales.OrderLine (OrderId, ProductId, Quantity, UnitPrice) VALUES (1, 100, 1, 1);\n"
                       "SET @a = SCOPE_IDENTITY();\nSET @b = @@IDENTITY;\nSET @c = IDENT_CURRENT('sales.OrderLine');")

    routine, session, table = of_kind(artefact, "last_identity")
    assert [n.attrs["scope"] for n in (routine, session, table)] == ["routine", "session", "table"]
    assert table.attrs["table"] == "RetailSample.sales.OrderLine"
    assert str(routine.type) == str(session.type) == str(table.type) == "decimal(precision=38, scale=0)"
    assert routine.attrs.get("table") is None


def test_ident_current_of_a_name_the_catalog_cannot_resolve_is_kept_as_written(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "DECLARE @c numeric(38, 0);\nSET @c = IDENT_CURRENT('sales.NoSuchTable');")

    [assign] = of_kind(artefact, "assign")
    assert assign.child("argument").kind == "opaque_fragment"
    assert "sales.NoSuchTable" in assign.child("argument").attrs["reason"]


def test_set_identity_insert_names_its_table(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "SET IDENTITY_INSERT sales.[Order] ON;\nSET IDENTITY_INSERT sales.[Order] OFF;")

    on, off = of_kind(artefact, "set_session_option")
    assert (on.attrs["option"], on.attrs["value"], on.attrs["table"]) == ("IDENTITY_INSERT", True, "RetailSample.sales.Order")
    assert off.attrs["value"] is False
    assert "RetailSample.sales.Order" in artefact.dependencies


# ---------------------------------------------------------------- the corpus

def test_the_ticket_that_left_these_constructs_as_barriers_is_not_named_in_any_reason(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, "BEGIN TRY SET ROWCOUNT 5; END TRY BEGIN CATCH SELECT 1; END CATCH")

    assert all("ticket" not in n.attrs["reason"] and "#" not in n.attrs["reason"]
               for n in of_kind(artefact, "opaque_fragment"))
