"""`TRY`/`CATCH` and the statements guarded for `@@ERROR` (spec section 6, "Errors"; ADR 0004), each compared with SQL Server
running the T-SQL the generated code stands for. A trace is the list of what the code printed, in order."""
import pytest

import migrt_spark as lib
from support_harness import C, error_of


def printed(duo) -> list[str]:
    """What the last batch PRINTed, in order."""
    return [m.text for m in duo.oracle._messages if m.number == 0]


def numbers(duo) -> list[int]:
    return [m.number for m in duo.oracle._messages if m.severity > 10]


def table_t(duo):
    duo.table("T", [C("Id", "int", nullable=False), C("Q", "int")], primary_key=("Id",),
              checks=(lib.Check("CK_T", "Q > 0", ("Q",)),))
    duo.tsql("INSERT {s}.T VALUES (1, 1)")
    return duo.target("T")


def seeded(duo):
    target = table_t(duo)
    ss = duo.session()
    ss.insert_values(target, [{"Id": 1, "Q": 1}])
    return ss, target


# ---------------------------------------------------------------------- what a CATCH sees

ERRORS = [
    ("a duplicate key", "INSERT {s}.T VALUES (1, 1)",
     lambda ss, t, spark, s: ss.insert_values(t, [{"Id": 1, "Q": 1}])),
    ("a check constraint", "INSERT {s}.T VALUES (2, 0)",
     lambda ss, t, spark, s: ss.insert_values(t, [{"Id": 2, "Q": 0}])),
    ("a division by zero", "SELECT 1 / 0", lambda ss, t, spark, s: spark.sql("SELECT 1 / 0").collect()),
    ("an overflow", "SELECT 2147483647 + 1",
     lambda ss, t, spark, s: spark.sql("SELECT CAST(2147483647 AS INT) + CAST(1 AS INT)").collect()),
    ("a subquery with two rows", "SELECT (SELECT a FROM (VALUES (1), (2)) AS v (a))",
     lambda ss, t, spark, s: spark.sql("SELECT (SELECT a FROM (VALUES (1), (2)) AS v (a))").collect()),
    ("a raised error", "RAISERROR('boom %s', 16, 3, 'x')", lambda ss, t, spark, s: ss.raise_error("boom %s", 16, 3, "x")),
    ("a thrown error", "THROW 50001, 'boom', 7", lambda ss, t, spark, s: ss.throw(50001, "boom", 7)),
]


@pytest.mark.parametrize("name, tsql, code", ERRORS, ids=[e[0] for e in ERRORS])
def test_a_catch_sees_what_sql_servers_catch_sees(duo, name, tsql, code):
    ss, target = seeded(duo)
    duo.tsql(f"BEGIN TRY {tsql}; PRINT 'not reached'; END TRY BEGIN CATCH "
             f"PRINT CONCAT('caught ', ERROR_NUMBER(), ' ', ERROR_SEVERITY(), ' ', ERROR_STATE(), ' ', ERROR_MESSAGE()); "
             f"END CATCH; PRINT 'after'")
    expected = printed(duo)

    trace = []
    with ss.try_catch() as t:
        code(ss, target, duo.spark, duo.schema)
        trace.append("not reached")
    if t.error is not None:
        with ss.handling(t.error):
            trace.append(f"caught {ss.error_number()} {ss.error_severity()} {ss.error_state()} {ss.error_message()}")
    trace.append("after")
    assert trace == expected


def test_a_conversion_failure_ends_the_batch_and_a_catch_still_sees_it(duo):
    ss = duo.session()
    duo.tsql("BEGIN TRY SELECT CAST('abc' AS int); END TRY BEGIN CATCH PRINT CONCAT('caught ', ERROR_NUMBER()); END CATCH")
    expected = printed(duo)
    with ss.try_catch() as t:
        duo.spark.sql("SELECT CAST('abc' AS INT)").collect()
    assert [f"caught {t.error.number}"] == expected == ["caught 245"]
    assert t.error.ends == "batch" and not t.error.exact_message              # Spark does not say the text was a varchar
    assert ss.inexact_errors == [t.error]                                     # and the session says it kept a stand-in text


def test_a_try_block_that_fails_nothing_leaves_no_error_and_does_not_touch_the_error_number(duo):
    ss, target = seeded(duo)
    error_of(lambda: ss.insert_values(target, [{"Id": 1, "Q": 1}]))
    before = ss.at_error
    with ss.try_catch() as t:
        pass
    assert t.error is None and ss.at_error == before == 2627


def test_the_error_number_after_a_catch_is_the_caught_errors(duo):
    ss, target = seeded(duo)
    duo.tsql("BEGIN TRY INSERT {s}.T VALUES (1, 1); END TRY BEGIN CATCH PRINT CONCAT('n=', @@ERROR); END CATCH")
    expected = printed(duo)
    with ss.try_catch() as t:
        ss.insert_values(target, [{"Id": 1, "Q": 1}])
    assert [f"n={ss.at_error}"] == expected
    assert t.error is not None and ss.at_error == t.error.number


def test_a_message_of_severity_ten_or_below_is_not_caught(duo):
    ss = duo.session()
    duo.tsql("BEGIN TRY RAISERROR('soft', 10, 1); PRINT 'went on'; END TRY BEGIN CATCH PRINT 'caught'; END CATCH")
    expected = printed(duo)
    trace = []
    with ss.try_catch() as t:
        ss.raise_error("soft", 10, 1)
        trace.append("went on")
    assert t.error is None and trace == ["went on"] and "caught" not in expected and expected[-1] == "went on"
    assert ss.messages == ["soft"]


def test_an_error_in_the_handler_is_not_caught_by_its_own_try(duo):
    ss, target = seeded(duo)
    with ss.try_catch() as t:
        ss.insert_values(target, [{"Id": 1, "Q": 1}])
    assert t.error is not None
    with pytest.raises(lib.TsqlError) as caught:
        with ss.handling(t.error):
            ss.raise_error("inside the handler", 16, 1)
    assert caught.value.message == "inside the handler"


def test_an_inner_try_catches_first_and_an_outer_one_sees_what_the_inner_one_lets_through(duo):
    ss, target = seeded(duo)
    with ss.try_catch() as outer:
        with ss.try_catch() as inner:
            ss.insert_values(target, [{"Id": 1, "Q": 1}])
        assert inner.error is not None and inner.error.number == 2627
    assert outer.error is None


def test_a_rethrow_raises_the_error_being_handled_unchanged_and_ends_the_batch(duo):
    ss = duo.session()
    expected = duo.tsql_error("BEGIN TRY RAISERROR('a', 11, 5); END TRY BEGIN CATCH THROW; END CATCH")
    with ss.try_catch() as t:
        ss.raise_error("a", 11, 5)
    with ss.handling(t.error):
        again = error_of(ss.rethrow)
    assert (again.number, again.severity, again.state, again.message) == (expected.number, expected.severity, expected.state,
                                                                          expected.text) == (50000, 11, 5, "a")
    assert again.ends == "batch"


def test_a_rethrow_of_a_system_error_keeps_its_number_severity_and_text(duo):
    ss, target = seeded(duo)
    expected = duo.tsql_error("BEGIN TRY INSERT {s}.T VALUES (1, 1); END TRY BEGIN CATCH THROW; END CATCH")
    with ss.try_catch() as t:
        ss.insert_values(target, [{"Id": 1, "Q": 1}])
    with ss.handling(t.error):
        again = error_of(ss.rethrow)
    assert (again.number, again.severity, again.state, again.message) == (expected.number, expected.severity, expected.state,
                                                                          expected.text)


def test_a_rethrow_outside_a_handler_is_refused_loudly(duo):
    ss = duo.session()
    with pytest.raises(lib.RethrowOutsideHandler):
        ss.rethrow()


# ---------------------------------------------------------------------- what a CATCH does not see (finding 7.6)

def test_a_missing_table_beside_the_try_is_not_caught_and_ends_the_batch(duo):
    """BEGIN TRY SELECT * FROM missing END TRY BEGIN CATCH ... : SQL Server stops with error 208 and the CATCH does not run."""
    ss = duo.session()
    duo.run("BEGIN TRY SELECT * FROM {s}.nosuch; END TRY BEGIN CATCH PRINT 'caught'; END CATCH; PRINT 'after'")
    assert numbers(duo) == [208] and printed(duo) == []
    with pytest.raises(lib.TsqlError) as stopped:
        with ss.try_catch():
            duo.spark.sql(f"SELECT * FROM {duo.schema}.nosuch").collect()
    assert stopped.value.number == 208
    assert stopped.value.ends == "scope" and stopped.value.depth == 1


def test_a_missing_column_beside_the_try_is_not_caught_either(duo):
    table_t(duo)
    ss = duo.session()
    duo.run("BEGIN TRY SELECT zzz FROM {s}.T; END TRY BEGIN CATCH PRINT 'caught'; END CATCH; PRINT 'after'")
    assert numbers(duo) == [207] and printed(duo) == []
    with pytest.raises(lib.TsqlError) as stopped:
        with ss.try_catch():
            duo.spark.sql(f"SELECT zzz FROM {duo.schema}.T").collect()
    assert stopped.value.number == 207


def test_the_same_missing_table_in_a_called_procedure_is_caught_by_the_callers_try(duo):
    ss = duo.session()
    duo.tsql("CREATE PROCEDURE {s}.Callee AS SELECT * FROM {s}.nosuch")
    duo.tsql("BEGIN TRY EXEC {s}.Callee; END TRY BEGIN CATCH PRINT CONCAT('caught ', ERROR_NUMBER()); END CATCH; PRINT 'after'")
    expected = printed(duo)
    trace = []
    with ss.try_catch() as t:
        with ss.scope("Callee"):
            duo.spark.sql(f"SELECT * FROM {duo.schema}.nosuch").collect()
    if t.error is not None:
        trace.append(f"caught {t.error.number}")
    trace.append("after")
    assert trace == expected == ["caught 208", "after"]


def test_the_missing_table_inside_the_procedure_is_not_caught_by_a_try_in_the_procedure_either(duo):
    ss = duo.session()
    with pytest.raises(lib.TsqlError) as stopped:
        with ss.scope("Callee"):
            with ss.try_catch():
                duo.spark.sql(f"SELECT * FROM {duo.schema}.nosuch").collect()
    assert stopped.value.number == 208 and stopped.value.depth == 2


# ---------------------------------------------------------------------- guarded statements (ADR 0004)

CUSTOMERS = """CREATE PROCEDURE {s}.LoadBatch @c1 varchar(10), @c2 varchar(10), @c3 varchar(10) AS
BEGIN
    SET NOCOUNT ON; SET XACT_ABORT OFF;
    DECLARE @err int, @loaded int, @failed int;
    SELECT @loaded = 0, @failed = 0;
    BEGIN TRANSACTION;
    INSERT {s}.Cust (Code) VALUES (@c1);
    SELECT @err = @@ERROR;
    IF @err = 0 SET @loaded = @loaded + 1 ELSE SET @failed = @failed + 1;
    INSERT {s}.Cust (Code) VALUES (@c2);
    SELECT @err = @@ERROR;
    IF @err = 0 SET @loaded = @loaded + 1 ELSE SET @failed = @failed + 1;
    INSERT {s}.Cust (Code) VALUES (@c3);
    SELECT @err = @@ERROR;
    IF @err = 0 SET @loaded = @loaded + 1 ELSE SET @failed = @failed + 1;
    IF @loaded = 0 GOTO nothing_loaded;
    COMMIT TRANSACTION;
    SELECT @loaded AS Loaded, @failed AS Failed;
    RETURN 0;
nothing_loaded:
    ROLLBACK TRANSACTION;
    SELECT 0 AS Loaded, @failed AS Failed;
    RETURN 1;
END"""


def customers(duo):
    duo.table("Cust", [C("Id", "int", nullable=False, identity=(1, 1)), C("Code", "varchar(10)", nullable=False)],
              primary_key=("Id",), uniques=(lib.Unique("UQ_Cust", ("Code",)),))
    duo.tsql(CUSTOMERS)
    duo.tsql("INSERT {s}.Cust (Code) VALUES ('dup1')")
    ss = duo.session()
    ss.insert_values(duo.target("Cust"), [{"Code": "dup1"}])
    return ss


def load_batch(ss, target, codes):
    """stg.uspLoadCustomerBatch, generated: every statement in a guarded block, because the procedure reads @@ERROR."""
    loaded = failed = 0
    with ss.transaction() as tx:
        for code in codes:
            with ss.guarded():
                ss.insert_values(target, [{"Code": code}])
            if ss.at_error == 0:
                loaded += 1
            else:
                failed += 1
        if loaded == 0:
            tx.rollback()
            return {"Loaded": 0, "Failed": failed}
        tx.commit()
    return {"Loaded": loaded, "Failed": failed}


@pytest.mark.parametrize("codes", [("a", "b", "c"), ("a", "dup1", "c"), ("dup1", "b", "dup1"), ("dup1", "dup1", "dup1"),
                                   ("x", "x", "y")], ids=["all new", "one duplicate", "two duplicates", "all duplicates",
                                                         "a duplicate within the batch"])
def test_a_procedure_that_reads_the_error_number_goes_on_after_a_failed_statement_as_the_original_does(duo, codes):
    ss = customers(duo)
    expected, _ = duo.run(f"EXEC {{s}}.LoadBatch '{codes[0]}', '{codes[1]}', '{codes[2]}'")
    actual = load_batch(ss, duo.target("Cust"), codes)
    assert actual == expected[0]
    duo.assert_same("Cust", ["Code"], "Code")


def test_a_statement_that_succeeds_inside_a_guard_sets_the_error_number_to_zero(duo):
    ss = customers(duo)
    with ss.guarded():
        ss.insert_values(duo.target("Cust"), [{"Code": "dup1"}])
    assert ss.at_error == 2627
    with ss.guarded():
        ss.insert_values(duo.target("Cust"), [{"Code": "new"}])
    assert ss.at_error == 0


def test_a_scope_ending_error_stops_the_procedure_and_the_caller_goes_on_with_the_error_number_set(duo):
    ss = duo.session()
    duo.tsql("CREATE PROCEDURE {s}.Callee AS BEGIN PRINT 'callee-1'; SELECT * FROM {s}.nosuch; PRINT 'callee-2'; END")
    duo.tsql("CREATE PROCEDURE {s}.Caller AS BEGIN EXEC {s}.Callee; PRINT CONCAT('caller after, error ', @@ERROR); END")
    duo.run("EXEC {s}.Caller")
    expected = printed(duo)
    trace = []

    def callee():
        with ss.scope("Callee"):
            trace.append("callee-1")
            with ss.guarded():
                duo.spark.sql(f"SELECT * FROM {duo.schema}.nosuch").collect()
            trace.append("callee-2")

    callee()
    trace.append(f"caller after, error {ss.at_error}")
    assert trace == expected == ["callee-1", "caller after, error 208"]


def test_a_batch_ending_error_stops_everything_even_in_a_guard(duo):
    ss = duo.session()
    duo.tsql("CREATE PROCEDURE {s}.Callee AS BEGIN PRINT 'callee-1'; THROW 50001, 'x', 1; PRINT 'callee-2'; END")
    duo.tsql("CREATE PROCEDURE {s}.Caller AS BEGIN EXEC {s}.Callee; PRINT 'caller after'; END")
    duo.run("EXEC {s}.Caller; PRINT 'batch after'")
    expected = printed(duo)
    trace = []
    with pytest.raises(lib.TsqlError):
        with ss.scope("Callee"):
            trace.append("callee-1")
            with ss.guarded():
                ss.throw(50001, "x", 1)
            trace.append("callee-2")
    assert trace == expected == ["callee-1"]


def test_with_xact_abort_on_a_guard_does_not_let_the_procedure_go_on(duo):
    ss = customers(duo)
    duo.tsql("CREATE PROCEDURE {s}.Abort AS BEGIN SET XACT_ABORT ON; PRINT 'before'; INSERT {s}.Cust (Code) VALUES ('dup1'); "
             "PRINT 'after'; END")
    duo.run("EXEC {s}.Abort")
    expected = printed(duo)
    trace = ["before"]
    ss.set_xact_abort(True)
    with pytest.raises(lib.TsqlError) as stopped:
        with ss.guarded():
            ss.insert_values(duo.target("Cust"), [{"Code": "dup1"}])
        trace.append("after")
    assert trace == expected == ["before"] and stopped.value.number == 2627
    ss.set_xact_abort(False)


def test_a_guard_inside_a_try_hands_the_error_to_the_catch(duo):
    ss = customers(duo)
    duo.tsql("BEGIN TRY INSERT {s}.Cust (Code) VALUES ('dup1'); PRINT 'next statement'; END TRY BEGIN CATCH PRINT 'caught'; END CATCH")
    expected = printed(duo)
    trace = []
    with ss.try_catch() as t:
        with ss.guarded():
            ss.insert_values(duo.target("Cust"), [{"Code": "dup1"}])
        trace.append("next statement")
    if t.error is not None:
        trace.append("caught")
    assert trace == expected == ["caught"]


@pytest.mark.parametrize("statement, tsql, code", [
    ("a duplicate key", "INSERT {s}.Cust (Code) VALUES ('dup1')",
     lambda ss, duo: ss.insert_values(duo.target("Cust"), [{"Code": "dup1"}])),
    ("a missing table", "SELECT * FROM {s}.nosuch",
     lambda ss, duo: duo.spark.sql(f"SELECT * FROM {duo.schema}.nosuch").collect()),
], ids=["a statement-ending error", "a scope-ending error"])
def test_a_guard_in_a_called_procedure_hands_the_error_to_the_callers_try(duo, statement, tsql, code):
    """A TRY anywhere up the call stack takes the error: the callee does not go on, whatever it reads of @@ERROR."""
    ss = customers(duo)
    duo.tsql(f"CREATE PROCEDURE {{s}}.Callee AS BEGIN PRINT 'callee-1'; {tsql}; PRINT 'callee-2'; END")
    duo.run("BEGIN TRY EXEC {s}.Callee; PRINT 'caller-went-on'; END TRY BEGIN CATCH PRINT CONCAT('caught ', ERROR_NUMBER()); END CATCH")
    expected = printed(duo)
    trace = []
    with ss.try_catch() as t:
        with ss.scope("Callee"):
            trace.append("callee-1")
            with ss.guarded():
                code(ss, duo)
            trace.append("callee-2")
        trace.append("caller-went-on")
    if t.error is not None:
        trace.append(f"caught {t.error.number}")
    assert trace == expected
    assert t.error is not None


def test_a_python_bug_inside_a_guard_is_not_swallowed(duo):
    ss = duo.session()
    with pytest.raises(TypeError):
        with ss.guarded():
            raise TypeError("a bug")


def test_an_unmapped_failure_in_a_guard_stops_the_procedure_and_is_recorded(duo):
    ss = duo.session()
    with pytest.raises(lib.TsqlError) as stopped:
        with ss.guarded():
            duo.spark.sql("SELECT * FROM parquet.`/nonexistent/path`").collect()
    assert stopped.value.unmapped and stopped.value.ends == "batch"
    assert ss.unmapped_errors == [stopped.value]


# ---------------------------------------------------------------------- the library's own errors are the same kind

def test_the_savepoint_and_transaction_requests_outside_a_transaction_are_sql_servers_errors(duo):
    from support_harness import assert_same_error

    ss = duo.session()
    for request, call in (("COMMIT TRANSACTION", lambda: ss._tx.commit()), ("ROLLBACK TRANSACTION", lambda: ss._tx.rollback()),
                          ("SAVE TRANSACTION s1", lambda: ss._tx.save("s1", None))):
        assert_same_error(duo, request, call)
