"""A scope-ending error inside a transaction leaves the transaction open (ADR 0004, amended; issue #85), compared with SQL Server.

With `XACT_ABORT` off an error that ends the scope of a procedure (a table that does not exist when the statement runs,
208) does not end the transaction the procedure opened: the caller goes on inside it, `@@TRANCOUNT` is unchanged, and
the caller may commit it or roll it back. With `XACT_ABORT` on the error ends the batch and rolls the transaction back.

The procedure is generated in both forms a transaction can take: statements (`begin_transaction()`, which lowering of a
`BEGIN TRAN` without a matching `COMMIT` needs) and a `transaction()` block."""
import pytest

import migrt_spark as lib
from support_harness import C, error_of

BEFORE = "BEGIN TRAN; INSERT {s}.T VALUES (2, 20); SELECT * FROM {s}.nosuch; INSERT {s}.T VALUES (3, 30)"


def table_t(duo):
    duo.table("T", [C("Id", "int", nullable=False), C("V", "int")], primary_key=("Id",))
    duo.tsql("INSERT {s}.T VALUES (1, 10)")
    ss = duo.session()
    ss.insert_values(duo.target("T"), [{"Id": 1, "V": 10}])
    duo.tsql("CREATE PROCEDURE {s}.Callee AS BEGIN " + BEFORE + "; END")
    return ss


def called(duo, sql):
    """A batch that may raise errors: its first result set, and the numbers of the errors it raised."""
    rows, errors = duo.run(sql)
    return rows, [e.number for e in errors]


def callee_statements(ss, duo):
    """BEGIN TRAN; INSERT; SELECT * FROM missing (guarded: the procedure reads @@ERROR); INSERT."""
    with ss.scope("Callee", procedure_call=True):
        ss.begin_transaction()
        ss.insert_values(duo.target("T"), [{"Id": 2, "V": 20}])
        with ss.guarded():
            duo.spark.sql(f"SELECT * FROM {duo.schema}.nosuch").collect()
        ss.insert_values(duo.target("T"), [{"Id": 3, "V": 30}])


def callee_block(ss, duo):
    """The same procedure with its transaction as a block."""
    with ss.scope("Callee", procedure_call=True):
        with ss.transaction():
            ss.insert_values(duo.target("T"), [{"Id": 2, "V": 20}])
            with ss.guarded():
                duo.spark.sql(f"SELECT * FROM {duo.schema}.nosuch").collect()
            ss.insert_values(duo.target("T"), [{"Id": 3, "V": 30}])


FORMS = [pytest.param(callee_statements, id="statements"), pytest.param(callee_block, id="a block")]


@pytest.mark.parametrize("callee", FORMS)
@pytest.mark.parametrize("ending", ["COMMIT", "ROLLBACK"])
def test_with_xact_abort_off_the_caller_goes_on_inside_the_open_transaction_and_ends_it(duo, callee, ending):
    ss = table_t(duo)
    [seen], errors = called(duo, "EXEC {s}.Callee; SELECT @@TRANCOUNT AS tc, @@ERROR AS er")
    assert errors == [208, 266]               # the procedure ended with a transaction it opened still open: 266 says so
    expected_rows = duo.sql_rows("T", ["Id", "V"], "Id")
    duo.tsql(ending)
    expected_after = duo.sql_rows("T", ["Id", "V"], "Id")
    assert seen == {"tc": 1, "er": 266} and expected_rows == [(1, 10), (2, 20)]

    with ss.guarded():                                          # the caller reads @@ERROR: error 266 ends only the call
        callee(ss, duo)
    assert (ss.trancount, ss.at_error) == (seen["tc"], seen["er"]) == (1, 266)
    assert duo.delta_rows("T", ["Id", "V"], "Id") == expected_rows
    (ss.commit_transaction if ending == "COMMIT" else ss.rollback_transaction)()
    assert ss.trancount == duo.tsql("SELECT @@TRANCOUNT AS tc")[0]["tc"] == 0
    assert duo.delta_rows("T", ["Id", "V"], "Id") == expected_after == ([(1, 10), (2, 20)] if ending == "COMMIT" else [(1, 10)])


@pytest.mark.parametrize("callee", FORMS)
def test_with_xact_abort_on_the_error_ends_the_batch_and_rolls_the_transaction_back(duo, callee):
    ss = table_t(duo)
    expected = duo.tsql_error("SET XACT_ABORT ON; EXEC {s}.Callee")
    [after] = duo.tsql("SELECT @@TRANCOUNT AS tc")
    expected_rows = duo.sql_rows("T", ["Id", "V"], "Id")
    assert (expected.number, after["tc"], expected_rows) == (208, 0, [(1, 10)])

    ss.set_xact_abort(True)
    raised = error_of(lambda: callee(ss, duo))
    assert (raised.number, raised.severity, raised.state) == (expected.number, expected.severity, expected.state)
    assert ss.trancount == after["tc"]
    assert duo.delta_rows("T", ["Id", "V"], "Id") == expected_rows


def test_a_caller_that_opened_its_own_transaction_first_keeps_it_open_with_the_callee_inside_it(duo):
    ss = table_t(duo)
    [seen], errors = called(duo, "BEGIN TRAN; EXEC {s}.Callee; SELECT @@TRANCOUNT AS tc")
    assert errors == [208, 266]
    duo.tsql("ROLLBACK")
    expected_after = duo.sql_rows("T", ["Id", "V"], "Id")

    ss.begin_transaction()
    with ss.guarded():
        callee_statements(ss, duo)
    assert ss.trancount == seen["tc"] == 2
    ss.rollback_transaction()
    assert ss.trancount == 0 and duo.delta_rows("T", ["Id", "V"], "Id") == expected_after == [(1, 10)]


def test_a_statement_ending_error_inside_a_block_still_lets_the_block_go_on_and_commit(duo):
    ss = table_t(duo)
    _, errors = called(duo, "BEGIN TRAN; INSERT {s}.T VALUES (1, 99); INSERT {s}.T VALUES (4, 40); COMMIT")
    assert errors == [2627]
    expected = duo.sql_rows("T", ["Id", "V"], "Id")
    with ss.transaction():
        with ss.guarded():
            ss.insert_values(duo.target("T"), [{"Id": 1, "V": 99}])           # a duplicate key ends only the statement
        ss.insert_values(duo.target("T"), [{"Id": 4, "V": 40}])
    assert duo.delta_rows("T", ["Id", "V"], "Id") == expected == [(1, 10), (4, 40)]
    assert ss.trancount == 0


def test_a_block_that_raises_an_error_that_is_not_a_scope_abort_still_rolls_back(duo):
    ss = table_t(duo)
    with pytest.raises(lib.TsqlError):
        with ss.transaction():
            ss.insert_values(duo.target("T"), [{"Id": 2, "V": 20}])
            ss.insert_values(duo.target("T"), [{"Id": 1, "V": 99}])
    assert ss.trancount == 0 and duo.delta_rows("T", ["Id", "V"], "Id") == [(1, 10)]


def test_the_statement_forms_of_the_transaction_statements_count_levels_as_sql_server_does(duo):
    ss = table_t(duo)
    counts = []
    ss.begin_transaction()
    counts.append(ss.trancount)
    ss.begin_transaction()
    counts.append(ss.trancount)
    ss.save_transaction("sp")
    ss.insert_values(duo.target("T"), [{"Id": 2, "V": 20}])
    ss.rollback_to_save_point("sp")
    counts.append(ss.trancount)
    ss.commit_transaction()
    counts.append(ss.trancount)
    ss.commit_transaction()
    counts.append(ss.trancount)
    expected = [duo.tsql("BEGIN TRAN; SELECT @@TRANCOUNT AS n")[0]["n"], duo.tsql("BEGIN TRAN; SELECT @@TRANCOUNT AS n")[0]["n"]]
    duo.tsql("SAVE TRAN sp; INSERT {s}.T VALUES (2, 20); ROLLBACK TRAN sp")
    expected += [duo.tsql("SELECT @@TRANCOUNT AS n")[0]["n"], duo.tsql("COMMIT; SELECT @@TRANCOUNT AS n")[0]["n"],
                 duo.tsql("COMMIT; SELECT @@TRANCOUNT AS n")[0]["n"]]
    assert counts == expected == [1, 2, 2, 1, 0]
    assert duo.delta_rows("T", ["Id", "V"], "Id") == duo.sql_rows("T", ["Id", "V"], "Id") == [(1, 10)]


def test_the_transaction_state_is_one_while_open_and_zero_otherwise(duo):
    ss = table_t(duo)
    seen = [ss.xact_state()]
    ss.begin_transaction()
    seen.append(ss.xact_state())
    ss.rollback_transaction()
    seen.append(ss.xact_state())
    expected = [duo.tsql("SELECT XACT_STATE() AS s")[0]["s"], duo.tsql("BEGIN TRAN; SELECT XACT_STATE() AS s")[0]["s"],
                duo.tsql("ROLLBACK; SELECT XACT_STATE() AS s")[0]["s"]]
    assert seen == expected == [0, 1, 0]
