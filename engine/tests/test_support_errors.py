"""The error helpers of the support library (spec section 6, "Errors"): `RAISERROR`, `THROW`, `ERROR_*()`, `TRY`/`CATCH` and
guarded statements, each compared with SQL Server running the same statements. Every test names the T-SQL it stands for."""
import pytest

import migrt_spark as lib
from support_harness import C, assert_same_error, error_of


# ---------------------------------------------------------------------- RAISERROR: the text

FORMATS = [
    ("v=%s", ["abc"]),
    ("%d %i %u", [5, -6, 7]),
    ("%o %x %X", [8, 255, 255]),
    ("%5d|%-5d|%05d", [6, 6, 8]),
    ("%+d % d %+d", [5, 5, -5]),
    ("%#x %#X %#o", [255, 255, 8]),
    ("%5s|%-5s|%.2s|%5.2s|", ["ab", "cd", "abcdef", "abcdef"]),
    ("%*d|%-*d|", [5, 3, 4, 2]),
    ("100%% sure %s", ["yes"]),
    ("%s and %d", [None, None]),
    ("too few %s %s", ["a"]),
    ("extra", ["a", 1]),
    ("%u %x", [-1, -1]),
    ("%d", [-2147483648]),
    ("unicode %s", ["é ü ß"]),
    ("%.0s|%5.0s|", ["abc", "abc"]),
    ("%s", ["it's"]),
]


def tsql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "N'" + value.replace("'", "''") + "'"
    return str(value)


@pytest.mark.parametrize("template, args", FORMATS, ids=[f[0] for f in FORMATS])
def test_raiserror_formats_its_text_as_sql_server_does(duo, template, args):
    ss = duo.session()
    literal = ", ".join(tsql_literal(a) for a in args)
    sql = f"RAISERROR(N'{template}', 16, 1{', ' if args else ''}{literal})"
    assert_same_error(duo, sql, lambda: ss.raise_error(template, 16, 1, *args))


@pytest.mark.parametrize("template, args", [
    ("%d", ["x"]), ("%s", [5]), ("%d", [3000000000]), ("%x", ["a"]), ("%5.2d", ["a"]),
], ids=["text for d", "number for s", "number past int for d", "text for x", "text for 5.2d"])
def test_raiserror_refuses_a_value_of_the_wrong_type_for_its_placeholder(duo, template, args):
    ss = duo.session()
    sql = f"RAISERROR('{template}', 16, 1, {tsql_literal(args[0])})"
    assert_same_error(duo, sql, lambda: ss.raise_error(template, 16, 1, *args))


@pytest.mark.parametrize("template", ["percent %", "%y", "%5.2z", "%", "%1!s! %2!s!", "ends with %-"])
def test_raiserror_refuses_a_placeholder_it_does_not_know(duo, template):
    ss = duo.session()
    assert_same_error(duo, f"RAISERROR('{template}', 16, 1, 1)", lambda: ss.raise_error(template, 16, 1, 1))


def test_raiserror_cuts_a_long_text_as_sql_server_does(duo):
    ss = duo.session()
    text = "a" * 2100
    expected = duo.tsql_error("DECLARE @m varchar(max) = REPLICATE(CAST('a' AS varchar(max)), 2100); RAISERROR(@m, 16, 1)")
    actual = error_of(lambda: ss.raise_error(text, 16, 1))
    assert actual.message == expected.text


# ---------------------------------------------------------------------- RAISERROR: severity, state, @@ERROR

# The client turns an error above severity 16 into an exception, not a message, so the oracle here goes no higher.
@pytest.mark.parametrize("severity", [11, 12, 15, 16])
def test_raiserror_above_ten_is_an_error_with_the_numbers_sql_server_gives(duo, severity):
    ss = duo.session()
    assert_same_error(duo, f"RAISERROR('s', {severity}, 4)", lambda: ss.raise_error("s", severity, 4))
    assert ss.at_error == 50000


@pytest.mark.parametrize("severity", [19, 20, 25, 26])
def test_raiserror_at_a_system_severity_is_refused_as_sql_server_refuses_it(duo, severity):
    ss = duo.session()
    assert_same_error(duo, f"RAISERROR('s', {severity}, 1)", lambda: ss.raise_error("s", severity, 1))


@pytest.mark.parametrize("state", [0, 1, 255, 256, 300, 1000, -1, -5])
def test_raiserror_keeps_a_state_the_way_sql_server_keeps_it(duo, state):
    ss = duo.session()
    assert_same_error(duo, f"RAISERROR('s', 11, {state})", lambda: ss.raise_error("s", 11, state))


@pytest.mark.parametrize("severity", [0, 1, 9, 10, -1, -20])
def test_raiserror_at_ten_or_below_is_a_message_and_never_an_exception(duo, severity):
    ss = duo.session()
    expected = duo.tsql(f"RAISERROR('soft %s', {severity}, 3, 'x'); SELECT @@ERROR AS e")
    ss.raise_error("soft %s", severity, 3, "x")                       # returns: nothing is raised
    assert ss.messages == ["soft x"]
    assert ss.at_error == expected[0]["e"] == 0
    shown = ss.message_records[-1]
    assert (shown.number, shown.severity, shown.state, shown.text) == (50000, max(severity, 0), 3, "soft x")


def test_raiserror_with_seterror_sets_the_error_number_even_for_a_message(duo):
    ss = duo.session()
    expected = duo.tsql("RAISERROR('soft', 10, 1) WITH SETERROR; SELECT @@ERROR AS e")[0]["e"]
    ss.raise_error("soft", 10, 1, seterror=True)
    assert ss.at_error == expected == 50000


def test_raiserror_sets_the_error_number_when_it_raises(duo):
    ss = duo.session()
    rows, _ = duo.run("RAISERROR('boom', 16, 1); SELECT @@ERROR AS e")
    expected = rows[0]["e"]
    error_of(lambda: ss.raise_error("boom", 16, 1))
    assert ss.at_error == expected


# ---------------------------------------------------------------------- THROW

def test_throw_keeps_the_number_the_message_and_the_state_and_fixes_the_severity_at_sixteen(duo):
    ss = duo.session()
    err = assert_same_error(duo, "THROW 50001, 'boom', 7", lambda: ss.throw(50001, "boom", 7))
    assert err.severity == 16 and err.number == 50001


@pytest.mark.parametrize("state", [0, 255, 256, 1000])
def test_throw_takes_the_state_modulo_256(duo, state):
    ss = duo.session()
    assert_same_error(duo, f"THROW 50001, 'x', {state}", lambda: ss.throw(50001, "x", state))


@pytest.mark.parametrize("number", [49999, 1, 0, -5])
def test_throw_refuses_a_number_below_fifty_thousand(duo, number):
    ss = duo.session()
    assert_same_error(duo, f"THROW {number}, 'x', 1", lambda: ss.throw(number, "x", 1))


def test_throw_accepts_the_largest_number(duo):
    ss = duo.session()
    assert_same_error(duo, "THROW 2147483647, 'x', 1", lambda: ss.throw(2147483647, "x", 1))


@pytest.mark.parametrize("message", ["plain", "100%% sure", "%%", "a%b", "%", "x%d y", "a %s b"])
def test_throw_reads_a_percent_sign_in_its_text_as_sql_server_does(duo, message):
    ss = duo.session()
    assert_same_error(duo, f"THROW 50001, '{message}', 1", lambda: ss.throw(50001, message, 1))


def test_throw_ends_the_batch_and_raiserror_ends_only_the_statement(duo):
    ss = duo.session()
    assert error_of(lambda: ss.throw(50001, "x", 1)).ends == "batch"
    assert error_of(lambda: ss.raise_error("x", 16, 1)).ends == "statement"


# ---------------------------------------------------------------------- ERROR_*() and @@ERROR

def test_the_error_functions_read_the_error_being_handled_and_are_null_outside_a_handler(duo):
    ss = duo.session()
    expected = duo.tsql("SELECT ERROR_NUMBER() AS n, ERROR_SEVERITY() AS s, ERROR_STATE() AS st, ERROR_MESSAGE() AS m")[0]
    assert (ss.error_number(), ss.error_severity(), ss.error_state(), ss.error_message()) == \
        (expected["n"], expected["s"], expected["st"], expected["m"]) == (None, None, None, None)
    err = error_of(lambda: ss.raise_error("a", 11, 5))
    with ss.handling(err):
        assert (ss.error_number(), ss.error_severity(), ss.error_state(), ss.error_message()) == (50000, 11, 5, "a")
        assert ss.error_field("number") == 50000


def test_the_error_functions_of_the_inner_handler_are_the_inner_error_and_the_outer_one_comes_back(duo):
    ss = duo.session()
    outer, inner = error_of(lambda: ss.raise_error("a", 11, 5)), error_of(lambda: ss.raise_error("inner", 12, 6))
    expected = duo.tsql(
        "BEGIN TRY RAISERROR('a', 11, 5); END TRY BEGIN CATCH BEGIN TRY RAISERROR('inner', 12, 6); END TRY "
        "BEGIN CATCH PRINT 'inner'; END CATCH; SELECT ERROR_NUMBER() AS n, ERROR_SEVERITY() AS s, ERROR_MESSAGE() AS m; "
        "END CATCH")
    with ss.handling(outer):
        with ss.handling(inner):
            assert ss.error_message() == "inner"
        assert (ss.error_number(), ss.error_severity(), ss.error_message()) == tuple(expected[0].values()) == (50000, 11, "a")


def test_error_line_and_error_procedure_are_what_the_generated_code_said_and_a_loud_error_when_it_said_nothing(duo):
    ss = duo.session()
    err = error_of(lambda: ss.raise_error("a", 11, 5))
    with ss.handling(err):
        with pytest.raises(lib.ErrorFieldUnknown):
            ss.error_line()
        assert ss.error_procedure() is None                                     # raised in a batch, outside any procedure
    with ss.scope():
        unnamed = error_of(lambda: ss.raise_error("a", 11, 5))
    with ss.handling(unnamed):
        with pytest.raises(lib.ErrorFieldUnknown):
            ss.error_procedure()
    with ss.scope("dbo.uspLog"):
        ss.statement_line = 12
        err = error_of(lambda: ss.raise_error("a", 11, 5))
    with ss.handling(err):
        assert (ss.error_line(), ss.error_procedure()) == (12, "dbo.uspLog")
    assert ss.error_line() is None and ss.error_procedure() is None            # outside a handler: NULL


def test_a_statement_that_succeeds_sets_the_error_number_back_to_zero(duo):
    duo.table("T", [C("Id", "int", nullable=False)], primary_key=("Id",))
    ss = duo.session()
    ss.insert_values(duo.target("T"), [{"Id": 1}])
    error_of(lambda: ss.insert_values(duo.target("T"), [{"Id": 1}]))
    assert ss.at_error == 2627
    ss.insert_values(duo.target("T"), [{"Id": 2}])
    assert ss.at_error == 0


# ---------------------------------------------------------------------- unmapped failures

def test_the_reserved_number_is_on_the_error_a_handler_reads_and_the_failure_is_in_the_session_log(duo):
    ss = duo.session()
    with ss.try_catch() as t:
        duo.spark.sql("SELECT * FROM parquet.`/nonexistent/path`").collect()
    assert t.error is not None and t.error.unmapped and t.error.number == ss.errors_reserved_number
    assert ss.unmapped_errors == [t.error]
    assert "PATH_NOT_FOUND" in t.error.message
    assert ss.at_error == t.error.number


def test_a_session_opened_without_the_source_mapping_fails_loudly_when_an_error_needs_it(duo, spark):
    ss = duo.session(errors=False)
    with pytest.raises(lib.ErrorMappingNotLoaded):
        ss.raise_error("x", 16, 1)


def test_a_python_bug_is_never_an_error_a_catch_could_see(duo):
    ss = duo.session()
    with pytest.raises(TypeError):
        with ss.try_catch():
            raise TypeError("a bug in the generated code")
    assert ss.unmapped_errors == []
