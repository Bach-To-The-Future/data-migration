"""The twins of procedural code (spec section 6, "Procedural code and twins"; issue #50): a value held in a Python
variable is `None` for NULL, so a comparison, a logical operator and integer arithmetic need T-SQL's rules on single
values. SQL Server is the oracle for every case, never the library's own tables: each result is what SQL Server returns
for the same operands, an error included.

Also here: the one result object a procedure gives back, the marker for a parameter's default, and the row count and
error number each statement leaves in the session (`statement_done`)."""
import itertools

import pytest

import migrt_spark as lib

VALUES = [None, 1, 2]
TYPES = [("tinyint", 8, False), ("smallint", 16, True), ("int", 32, True), ("bigint", 64, True)]
# Arithmetic on tinyint and smallint gives an int, so only int and bigint are types its result can overflow.
RESULT_TYPES = [t for t in TYPES if t[1] >= 32]
EDGES = {8: [0, 1, 254, 255], 16: [-32768, -1, 32766, 32767], 32: [-2147483648, -1, 2147483646, 2147483647],
         64: [-9223372036854775808, -1, 9223372036854775806, 9223372036854775807]}


def sql(value) -> str:
    return "NULL" if value is None else str(value)


def truth(value) -> str:
    return "NULL" if value is None else str(int(value))


# ---------------------------------------------------------------- three-valued logic

@pytest.mark.parametrize("operator", ["=", "<>", ">", "<", ">=", "<="])
def test_a_comparison_with_null_is_unknown_as_in_sql_server(duo, operator):
    ss = duo.session()
    for a, b in itertools.product(VALUES, VALUES):
        [row] = duo.tsql(f"SELECT CASE WHEN {sql(a)} {operator} {sql(b)} THEN 1 WHEN NOT ({sql(a)} {operator} {sql(b)}) "
                         "THEN 0 END AS r")
        assert ss.twin.compare(operator, a, b) == (None if row["r"] is None else bool(row["r"])), (a, operator, b)


def test_and_or_not_follow_sql_servers_truth_tables(duo):
    ss = duo.session()
    states = {True: "(1 = 1)", False: "(1 = 0)", None: "(1 = NULL)"}
    for a, b in itertools.product(states, states):
        for name, operator in (("and_", "AND"), ("or_", "OR")):
            [row] = duo.tsql(f"SELECT CASE WHEN {states[a]} {operator} {states[b]} THEN 1 "
                             f"WHEN NOT ({states[a]} {operator} {states[b]}) THEN 0 END AS r")
            expected = None if row["r"] is None else bool(row["r"])
            assert getattr(ss.twin, name)(a, b) == expected, (a, operator, b)
    for a in states:
        [row] = duo.tsql(f"SELECT CASE WHEN NOT {states[a]} THEN 1 WHEN NOT (NOT {states[a]}) THEN 0 END AS r")
        assert ss.twin.not_(a) == (None if row["r"] is None else bool(row["r"])), a


def test_a_condition_that_is_unknown_is_not_true_in_an_if_or_a_while(duo):
    ss = duo.session()
    assert [ss.twin.holds(v) for v in (True, False, None)] == [True, False, False]
    [row] = duo.tsql("DECLARE @r int = 0; IF NULL = 1 SET @r = 1; SELECT @r AS r")
    assert row["r"] == 0


def test_is_null_never_returns_unknown(duo):
    ss = duo.session()
    for value in VALUES:
        [row] = duo.tsql(f"SELECT CASE WHEN {sql(value)} IS NULL THEN 1 ELSE 0 END AS r")
        assert ss.twin.is_null(value) is bool(row["r"])


# ---------------------------------------------------------------- integer arithmetic

@pytest.mark.parametrize("name,sign", [("add", "+"), ("subtract", "-"), ("multiply", "*")])
@pytest.mark.parametrize("tsql_type,bits,signed", RESULT_TYPES)
def test_integer_arithmetic_gives_sql_servers_value_or_its_overflow_error(duo, name, sign, tsql_type, bits, signed):
    ss = duo.session()
    operands = EDGES[bits] + [None]
    for a, b in itertools.product(operands, operands):
        rows, errors = duo.run(f"SELECT CAST({sql(a)} AS {tsql_type}) {sign} CAST({sql(b)} AS {tsql_type}) AS r")
        if errors:
            with pytest.raises(lib.TsqlError) as raised:
                getattr(ss.twin, name)(a, b, bits=bits, signed=signed)
            assert raised.value.number == errors[0].number, (a, sign, b)
        else:
            assert getattr(ss.twin, name)(a, b, bits=bits, signed=signed) == rows[0]["r"], (a, sign, b)


@pytest.mark.parametrize("name,sign", [("integer_divide", "/"), ("modulo", "%")])
@pytest.mark.parametrize("tsql_type,bits,signed", RESULT_TYPES)
def test_integer_division_and_remainder_truncate_toward_zero_and_raise_where_sql_server_raises(
        duo, name, sign, tsql_type, bits, signed):
    ss = duo.session()
    operands = EDGES[bits] + [None, 0, 7, -7, 2, -2]
    for a, b in itertools.product(operands, operands):
        rows, errors = duo.run(f"SELECT CAST({sql(a)} AS {tsql_type}) {sign} CAST({sql(b)} AS {tsql_type}) AS r")
        if errors:
            with pytest.raises(lib.TsqlError) as raised:
                getattr(ss.twin, name)(a, b, bits=bits, signed=signed)
            assert raised.value.number == errors[0].number, (a, sign, b)
        else:
            assert getattr(ss.twin, name)(a, b, bits=bits, signed=signed) == rows[0]["r"], (a, sign, b)


def test_an_overflow_sets_the_error_number_and_names_the_type(duo):
    ss = duo.session()
    error = duo.tsql_error("SELECT CAST(2147483647 AS int) + CAST(1 AS int)")
    with pytest.raises(lib.TsqlError) as raised:
        ss.twin.add(2147483647, 1, bits=32, signed=True)
    assert raised.value.number == error.number == ss.at_error
    assert raised.value.message == error.text


def test_a_value_converted_to_an_integer_type_is_checked_against_its_range(duo):
    ss = duo.session()
    for tsql_type, bits, signed in TYPES:
        for value in EDGES[bits] + [None, 300, -1, 70000, 2147483648]:
            rows, errors = duo.run(f"SELECT CAST(CAST({sql(value)} AS bigint) AS {tsql_type}) AS r")
            if errors:
                with pytest.raises(lib.TsqlError) as raised:
                    ss.twin.to_integer(value, bits=bits, signed=signed)
                if bits >= 32:
                    assert raised.value.number == errors[0].number, (tsql_type, value)
                else:
                    # SQL Server has numbers of its own for the smaller types, which the source half does not cover
                    # yet: the failure is flagged with the reserved number, never given another error's number.
                    assert raised.value.unmapped and raised.value.number == ss.errors_reserved_number, (tsql_type, value)
            else:
                assert ss.twin.to_integer(value, bits=bits, signed=signed) == rows[0]["r"], (tsql_type, value)


# ---------------------------------------------------------------- what a procedure gives back, and defaults

def test_a_procedure_gives_back_one_result_object_of_a_code_its_output_parameters_and_its_result_sets(duo):
    ss = duo.session()
    frame = duo.spark.range(2)

    result = ss.result(7, {"total": 3}, [frame])

    assert (result.return_code, result.outputs, result.result_sets) == (7, {"total": 3}, [frame])
    empty = ss.result(0, {}, [])
    assert (empty.return_code, empty.outputs, empty.result_sets) == (0, {}, [])
    # a procedure's code is an integer, and `RETURN NULL` gives zero (compared with SQL Server in test_procedural_generation)
    assert ss.result(None, {}, []).return_code == 0


def test_the_marker_for_a_default_is_not_a_value_a_procedure_can_hold():
    assert lib.DEFAULT is Ellipsis
    assert all(v is not lib.DEFAULT for v in (None, 0, "", False))


# ---------------------------------------------------------------- what a statement leaves in @@ROWCOUNT and @@ERROR

def test_a_statement_sets_the_row_count_and_resets_the_error_unless_it_keeps_them(duo):
    ss = duo.session()
    ss.rowcount, ss.at_error = 9, 515

    ss.statement_done(None, reset_error=False)
    assert (ss.rowcount, ss.at_error) == (9, 515)                  # keeps both
    ss.statement_done(None)
    assert (ss.rowcount, ss.at_error) == (9, 0)                    # keeps the count, resets the error
    ss.statement_done(4)
    assert (ss.rowcount, ss.at_error) == (4, 0)
    ss.statement_done(0, reset_error=False)
    assert (ss.rowcount, ss.at_error) == (0, 0)


# ---------------------------------------------------------------- negation, COALESCE, BETWEEN and IN (issue #127)

# SQL Server's unary minus keeps the type of its operand except for tinyint, which gives smallint (it has no negative values).
NEGATION_RESULT = {"tinyint": (16, True), "smallint": (16, True), "int": (32, True), "bigint": (64, True)}


@pytest.mark.parametrize("tsql_type", ["tinyint", "smallint", "int", "bigint"])
def test_negation_gives_sql_servers_value_or_its_overflow_error(duo, tsql_type):
    bits, signed = NEGATION_RESULT[tsql_type]
    ss = duo.session()
    operand_bits = {"tinyint": 8, "smallint": 16, "int": 32, "bigint": 64}[tsql_type]
    for value in EDGES[operand_bits] + [None, 0, 1, 5]:
        rows, errors = duo.run(f"SELECT -CAST({sql(value)} AS {tsql_type}) AS r")
        if errors:
            with pytest.raises(lib.TsqlError) as raised:
                ss.twin.negate(value, bits=bits, signed=signed)
            if bits >= 32:
                assert raised.value.number == errors[0].number, (tsql_type, value)
            else:       # the source half does not cover the smaller integers yet: the reserved number, flagged
                assert raised.value.unmapped and raised.value.number == ss.errors_reserved_number, (tsql_type, value)
        else:
            assert ss.twin.negate(value, bits=bits, signed=signed) == rows[0]["r"], (tsql_type, value)


def test_coalesce_gives_the_first_value_that_is_not_null(duo):
    ss = duo.session()
    for values in itertools.product(VALUES, repeat=3):
        [row] = duo.tsql(f"SELECT COALESCE({', '.join(sql(v) for v in values)}, CAST(NULL AS int)) AS r")
        assert ss.twin.first_non_null(*values) == row["r"], values


def test_between_is_true_only_when_both_comparisons_are_and_unknown_when_a_bound_is_null_and_the_other_comparison_does_not_fail(duo):
    ss = duo.session()
    values = [None, 1, 2, 3]
    for a, low, high in itertools.product(values, repeat=3):
        for negated in (False, True):
            test = f"{sql(a)} {'NOT ' if negated else ''}BETWEEN {sql(low)} AND {sql(high)}"
            [row] = duo.tsql(f"SELECT CASE WHEN {test} THEN 1 WHEN NOT ({test}) THEN 0 END AS r")
            got = ss.twin.between(a, low, high)
            got = ss.twin.not_(got) if negated else got
            assert got == (None if row["r"] is None else bool(row["r"])), test


def test_in_is_unknown_when_nothing_matches_and_the_list_or_the_value_is_null(duo):
    ss = duo.session()
    for a in VALUES:
        for size in (1, 2, 3):
            for items in itertools.product(VALUES, repeat=size):
                for negated in (False, True):
                    test = f"{sql(a)} {'NOT ' if negated else ''}IN ({', '.join(sql(i) for i in items)})"
                    [row] = duo.tsql(f"SELECT CASE WHEN {test} THEN 1 WHEN NOT ({test}) THEN 0 END AS r")
                    got = ss.twin.in_list(a, *items)
                    got = ss.twin.not_(got) if negated else got
                    assert got == (None if row["r"] is None else bool(row["r"])), test
