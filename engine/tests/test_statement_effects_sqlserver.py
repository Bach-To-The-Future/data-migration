"""What each statement leaves in `@@ROWCOUNT` and `@@ERROR`, decided against SQL Server (`statement_effects`).

Every case is T-SQL run on the sandbox SQL Server in one session: first something sets the value (an `UPDATE` of three
rows for `@@ROWCOUNT`, a `RAISERROR` for `@@ERROR`), then the statement runs, then the values are read. The statement is
also lowered, and the effect the table gives its node must agree with what SQL Server left. So the table is checked
against the oracle, case by case, and every kind of statement the table lists has a case."""
import os

import pytest
from procedural_helpers import proc

from migrt.sources.sqlserver import statement_effects
from migrt.sources.sqlserver.connection import Connection

SEED_ROWS = 3

SOURCE = "CREATE TABLE #src (a int); INSERT #src VALUES (1), (2), (3);"
CURSOR = "DECLARE @v int; DECLARE c CURSOR LOCAL STATIC FOR SELECT a FROM #src WHERE a <= 1;"
CALLEE = "CREATE PROCEDURE #callee AS UPDATE #src SET a = a WHERE a <= 2;"


class Case:
    def __init__(self, kind, setup, statement, *, n=None, prior_rows=SEED_ROWS, prior_error=50000, error_statement=None,
                 after=None, find=None):
        self.kind, self.setup, self.statement = kind, setup, statement
        self.n, self.prior_rows, self.prior_error = n, prior_rows, prior_error
        self.error_statement = error_statement or statement
        self.after, self.find = after, find or kind


CASES = [
    Case("declare_variable", "", "DECLARE @x int;"),
    Case("declare_variable", "", "DECLARE @x int = 7;", find="declare_variable"),
    Case("declare_row_set_variable", "", "DECLARE @t TABLE (a int);"),
    Case("assign", "DECLARE @x int;", "SET @x = 1;"),
    Case("assign_from_rows", f"{SOURCE} DECLARE @x int;", "SELECT @x = a FROM #src WHERE a <= 2;", n=2),
    Case("assign_from_rows", f"{SOURCE} DECLARE @x int;", "SELECT @x = a FROM #src WHERE a > 100;", n=0),
    Case("create_temp_table", "", "CREATE TABLE #made (a int);"),
    Case("create_temp_table", SOURCE, "SELECT a INTO #made FROM #src WHERE a <= 2;", n=2),
    Case("drop_temp_table", "CREATE TABLE #made (a int);", "DROP TABLE #made;"),
    Case("if", "", "IF 1 = 0 PRINT 'x';"),
    Case("while", "", "WHILE 1 = 0 PRINT 'x';"),
    Case("break", SOURCE, "WHILE 1 = 1 BEGIN UPDATE #src SET a = a WHERE a <= 2; BREAK; END", prior_rows=2,
         error_statement="WHILE 1 = 1 BEGIN RAISERROR('again', 11, 1); BREAK; END"),
    Case("continue", "DECLARE @i int = 0;", "WHILE @i < 1 BEGIN SET @i += 1; CONTINUE; END"),
    Case("jump", "", "GOTO l2;\nl2:", find="jump"),
    Case("declare_label", "", "GOTO l2;\nl2:"),
    Case("emit_result_set", SOURCE, "SELECT a FROM #src;", n=3),
    Case("insert_rows", SOURCE, "INSERT #src (a) VALUES (7), (8);", n=2),
    Case("update_rows", SOURCE, "UPDATE #src SET a = a WHERE a <= 2;", n=2),
    Case("delete_rows", SOURCE, "DELETE #src WHERE a <= 1;", n=1),
    Case("call_procedure", SOURCE, "EXEC #callee;", n=2),
    Case("begin_transaction", "", "BEGIN TRAN;", after="WHILE @@TRANCOUNT > 0 COMMIT;"),
    Case("commit_transaction", "BEGIN TRAN;", "COMMIT;"),
    Case("rollback_transaction", "BEGIN TRAN;", "ROLLBACK;"),
    Case("save_point", "BEGIN TRAN;", "SAVE TRAN sp;", after="WHILE @@TRANCOUNT > 0 COMMIT;"),
    Case("rollback_to_save_point", "BEGIN TRAN; SAVE TRAN sp;", "ROLLBACK TRAN sp;", after="WHILE @@TRANCOUNT > 0 COMMIT;"),
    Case("set_isolation_level", "", "SET TRANSACTION ISOLATION LEVEL READ COMMITTED;"),
    Case("declare_cursor", SOURCE + " DECLARE @v int;", "DECLARE c CURSOR LOCAL STATIC FOR SELECT a FROM #src WHERE a <= 1;"),
    Case("open_cursor", f"{SOURCE} {CURSOR}", "OPEN c;"),
    Case("fetch_cursor", f"{SOURCE} {CURSOR} OPEN c;", "FETCH NEXT FROM c INTO @v;", n=1),
    Case("fetch_cursor", f"{SOURCE} {CURSOR} OPEN c; FETCH NEXT FROM c INTO @v;", "FETCH NEXT FROM c INTO @v;", n=0),
    Case("close_cursor", f"{SOURCE} {CURSOR} OPEN c;", "CLOSE c;"),
    Case("deallocate_cursor", f"{SOURCE} {CURSOR}", "DEALLOCATE c;"),
    Case("try_catch", "", "BEGIN TRY DECLARE @y int; END TRY BEGIN CATCH PRINT 'x'; END CATCH"),
    Case("raise_error", "", "RAISERROR('soft', 10, 1);"),
    Case("emit_message", "", "PRINT 'x';"),
    Case("set_session_option", "", "SET NOCOUNT ON;"),
    Case("set_abort_on_error", "", "SET XACT_ABORT OFF;"),
]


def case_id(case: Case) -> str:
    return f"{case.kind}: {case.statement[:50]}"


def lowered_effect(case: Case, corpus_snapshot):
    """The effect the table gives the node of the statement under test, in the lowered case."""
    if case.kind == "call_procedure":                    # a temp procedure is not in the catalog: lower a catalog one
        artefact, _ = proc(corpus_snapshot, "EXEC sales.uspCloseStaleOrders @AsOf = '2020-01-01';")
    else:
        artefact, _ = proc(corpus_snapshot, f"{case.setup}\n{case.statement}")
    found = [n for n in artefact.nodes() if n.kind == case.find]
    assert found, f"the case lowers to no {case.find}: {[n.kind for n in artefact.nodes()]}"
    return statement_effects.effect(found[-1])


@pytest.fixture(scope="module")
def server():
    conn = Connection(os.environ.get("MSSQL_HOST", "sqlserver"), 1433, "sa", os.environ["MSSQL_SA_PASSWORD"], "tempdb")
    conn._errors = lambda: []                                 # type: ignore[method-assign]
    yield conn
    conn.close()


def run(conn, sql: str):
    return conn.capture(sql)[1]


def clean(conn) -> None:
    run(conn, "WHILE @@TRANCOUNT > 0 ROLLBACK;")
    for name in ("#src", "#made", "#seed", "#read"):
        run(conn, f"IF OBJECT_ID('tempdb..{name}') IS NOT NULL DROP TABLE {name};")
    run(conn, "IF OBJECT_ID('tempdb..#callee') IS NOT NULL DROP PROCEDURE #callee;")


def left_by(conn, case: Case, before: str, statement: str, read: str) -> dict:
    """One batch: set the value, run the statement, read what it left into a table; then fetch it."""
    clean(conn)
    if case.kind == "call_procedure":
        run(conn, CALLEE)
    run(conn, f"CREATE TABLE #seed (a int); INSERT #seed VALUES (1), (2), (3);\n{case.setup}\n{before}\n{statement}\n"
              f"{read} INTO #read;\n{case.after or ''}")
    [row] = run(conn, "SELECT * FROM #read")
    clean(conn)
    return row




@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_the_row_count_a_statement_leaves_is_what_sql_server_leaves(server, corpus_snapshot, case):
    effect = lowered_effect(case, corpus_snapshot)
    row = left_by(server, case, "UPDATE #seed SET a = a;", case.statement, "SELECT @@ROWCOUNT AS rc")
    expected = {"keeps": case.prior_rows, "zero": 0, "one": 1}.get(effect.rows, case.n)
    assert row["rc"] == expected, f"{effect} but SQL Server left {row['rc']}"


@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_the_error_number_a_statement_leaves_is_what_sql_server_leaves(server, corpus_snapshot, case):
    effect = lowered_effect(case, corpus_snapshot)
    row = left_by(server, case, "RAISERROR('seed', 11, 1);", case.error_statement, "SELECT @@ERROR AS er")
    assert row["er"] == {"keeps": case.prior_error, "reset": 0}[effect.error], f"{effect} but SQL Server left {row['er']}"


def test_every_kind_of_statement_in_the_table_has_a_case_unless_it_is_kept_as_written():
    kept_as_written = {"opaque_fragment", "dynamic_sql_execution", "schema_change", "permission_change"}
    # `return` is seen through a call, `rethrow` ends the batch and `write_current_row` needs a cursor over a catalog table
    not_run_here = {"return", "rethrow", "write_current_row"}
    assert {c.kind for c in CASES} | kept_as_written | not_run_here == statement_effects.statement_kinds()


def test_a_kind_kept_as_written_has_an_unknown_effect_and_nothing_is_assumed_for_it():
    for kind in ("opaque_fragment", "dynamic_sql_execution", "schema_change", "permission_change"):
        assert statement_effects.BY_KIND[kind] == statement_effects.UNKNOWN
