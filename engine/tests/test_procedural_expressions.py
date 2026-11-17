"""Negation, COALESCE, BETWEEN and IN in generated code (issue #127, spec section 6): each is generated in procedural code (Python
values, `None` is NULL) and in functions and views (columns), and gives what SQL Server gives for the same operands, an error
included. SQL Server is the oracle: every expected value below is what it returned, never a table written here."""
import itertools

import pytest

import migrt_spark as lib
from generation_helpers import generate_text
from lowering_helpers import lower_text, parameter
from migrt.targets.pyspark import generator
from test_procedural_generation import procedure, sql_literal

INT_EDGES = [None, -2147483648, -1, 0, 1, 2147483647]


def outcome_on_sql_server(duo, create: str, name: str, arguments: dict):
    """What the procedure gives on SQL Server: ("code", n) or ("error", number)."""
    duo.tsql(create.replace("sales.", "{s}."))
    passed = ", ".join(f"@{n} = {sql_literal(v)}" for n, v in arguments.items())
    rows, errors = duo.run(f"DECLARE @rc int; EXEC @rc = {{s}}.{name} {passed}; SELECT @rc AS rc")
    return ("error", errors[0].number) if errors else ("code", rows[0]["rc"])


def outcome_on_target(duo, function, arguments: dict):
    try:
        return ("code", function(duo.session(), **arguments).return_code)
    except lib.TsqlError as raised:
        return ("error", raised.number)


# ---------------------------------------------------------------- lowering: the type of a negation

def test_the_negation_of_a_tinyint_is_a_smallint_as_in_sql_server(corpus_snapshot):
    artefact, _ = lower_text(corpus_snapshot, "CREATE PROCEDURE sales.uspTest @t tinyint AS BEGIN\nRETURN -@t;\nEND",
                             kind="procedure", name="uspTest", parameters=[_tinyint("@t")])

    [negate] = [n for n in artefact.nodes() if n.kind == "negate"]

    assert negate.value_type.name == "integer" and negate.value_type.int_param("bits") == 16 and negate.value_type.flag("signed")


def _tinyint(name: str) -> dict:
    return {"name": name, "declared_type": "tinyint", "system_type": "tinyint", "max_length": 1, "precision": 3, "scale": 0,
            "direction": "input", "readonly": False, "nullable": True, "default": None}


# ---------------------------------------------------------------- procedural code (values)

NEGATE = "CREATE PROCEDURE sales.uspNegate @a int AS BEGIN\nRETURN -@a;\nEND"


@pytest.mark.parametrize("a", INT_EDGES, ids=repr)
def test_a_negated_integer_gives_sql_servers_value_or_its_overflow_error(corpus_snapshot, duo, a):
    file, function = procedure(corpus_snapshot, NEGATE, [parameter("@a")], name="uspNegate")

    assert outcome_on_target(duo, function, {"a": a}) == outcome_on_sql_server(duo, NEGATE, "uspNegate", {"a": a})
    assert file.file.manual_items == []


COALESCE = "CREATE PROCEDURE sales.uspCoalesce @a int, @b int, @c int AS BEGIN\nRETURN COALESCE(@a, @b, @c, 99);\nEND"


@pytest.mark.parametrize("values", list(itertools.product([None, 1, 2], repeat=3)), ids=repr)
def test_coalesce_gives_the_first_value_that_is_not_null(corpus_snapshot, duo, values):
    file, function = procedure(corpus_snapshot, COALESCE, [parameter("@a"), parameter("@b"), parameter("@c")],
                               name="uspCoalesce")
    arguments = dict(zip("abc", values))

    assert outcome_on_target(duo, function, arguments) == outcome_on_sql_server(duo, COALESCE, "uspCoalesce", arguments)
    assert file.file.manual_items == []


BETWEEN = ("CREATE PROCEDURE sales.uspBetween @a int, @lo int, @hi int AS BEGIN\n"
           "IF @a BETWEEN @lo AND @hi RETURN 1;\nIF @a NOT BETWEEN @lo AND @hi RETURN 2;\nRETURN 3;\nEND")


@pytest.mark.parametrize("values", list(itertools.product([None, 1, 2, 3], repeat=3)), ids=repr)
def test_between_and_not_between_are_three_valued(corpus_snapshot, duo, values):
    file, function = procedure(corpus_snapshot, BETWEEN, [parameter("@a"), parameter("@lo"), parameter("@hi")],
                               name="uspBetween")
    arguments = dict(zip(("a", "lo", "hi"), values))

    assert outcome_on_target(duo, function, arguments) == outcome_on_sql_server(duo, BETWEEN, "uspBetween", arguments)
    assert file.file.manual_items == []


IN_LIST = ("CREATE PROCEDURE sales.uspIn @a int, @b int, @c int AS BEGIN\n"
           "IF @a IN (@b, @c, 7) RETURN 1;\nIF @a NOT IN (@b, @c, 7) RETURN 2;\nRETURN 3;\nEND")


@pytest.mark.parametrize("values", list(itertools.product([None, 1, 7], repeat=3)), ids=repr)
def test_in_and_not_in_are_unknown_when_nothing_matches_and_a_value_is_null(corpus_snapshot, duo, values):
    file, function = procedure(corpus_snapshot, IN_LIST, [parameter("@a"), parameter("@b"), parameter("@c")], name="uspIn")
    arguments = dict(zip("abc", values))

    assert outcome_on_target(duo, function, arguments) == outcome_on_sql_server(duo, IN_LIST, "uspIn", arguments)
    assert file.file.manual_items == []


def test_a_string_between_or_in_is_manual_work_that_waits_for_the_comparison_helpers(corpus_snapshot):
    sql = ("CREATE PROCEDURE sales.uspTest @s varchar(10) AS BEGIN\nIF @s BETWEEN 'a' AND 'm' RETURN 1;\n"
           "IF @s IN ('x', 'y') RETURN 2;\nRETURN 3;\nEND")
    file = generate_text(corpus_snapshot, sql, kind="procedure", name="uspTest",
                         parameters=[parameter("@s", "varchar", length=10)])

    assert file.layer1.passed, file.layer1.failures
    assert len(file.file.manual_items) == 2 and {m.waits_for for m in file.file.manual_items} == {("#47",)}
    assert not any("#" in m.reason for m in file.file.manual_items)


# ---------------------------------------------------------------- procedural code: other types, strings, ISNULL

SQL_TYPES = {"int": 32, "bigint": 64, "smallint": 16}
EXTREMES = {"int": [None, -2147483648, -1, 0, 1, 2147483647],
            "bigint": [None, -9223372036854775808, -2147483648, -1, 0, 2147483647, 9223372036854775807],
            "smallint": [None, -32768, -1, 0, 1, 32767]}


def output_on_sql_server(duo, create: str, name: str, arguments: dict, kind: str):
    """What a procedure with one output parameter `@r` gives on SQL Server: ("ok", value) or ("error", number)."""
    duo.tsql(create.replace("sales.", "{s}."))
    passed = ", ".join(f"@{n} = {sql_literal(v)}" for n, v in arguments.items())
    rows, errors = duo.run(f"DECLARE @r {kind}; EXEC {{s}}.{name} {passed}{', ' if passed else ''}@r = @r OUTPUT; SELECT @r AS r")
    return ("error", errors[0].number) if errors else ("ok", rows[0]["r"])


def output_on_target(duo, function, arguments: dict):
    try:
        return ("ok", function(duo.session(), r=None, **arguments).outputs["r"])
    except lib.TsqlError as raised:
        return ("error", raised.number)


def same_outcome(target, sql_server, bits: int = 32):
    """The two outcomes agree; the error of a type smaller than 32 bits is the reserved number, which the source half does not
    cover yet, so for it only that both fail is compared."""
    if target[0] == "error" and bits < 32:
        return sql_server[0] == "error"
    return target == sql_server


@pytest.mark.parametrize("kind", sorted(SQL_TYPES))
def test_negation_of_each_integer_type_gives_sql_servers_value_or_its_overflow_error(corpus_snapshot, duo, kind):
    create = f"CREATE PROCEDURE sales.uspNeg @a {kind}, @r {kind} OUTPUT AS BEGIN\nSET @r = -@a;\nEND"
    file, function = procedure(corpus_snapshot, create, [parameter("@a", kind), parameter("@r", kind, direction="output")],
                               name="uspNeg")
    assert file.file.manual_items == []
    duo.tsql(create.replace("sales.", "{s}."))

    for a in EXTREMES[kind]:
        passed = f"@a = {sql_literal(a)}"
        rows, errors = duo.run(f"DECLARE @r {kind}; EXEC {{s}}.uspNeg {passed}, @r = @r OUTPUT; SELECT @r AS r")
        expected = ("error", errors[0].number) if errors else ("ok", rows[0]["r"])
        assert same_outcome(output_on_target(duo, function, {"a": a}), expected, SQL_TYPES[kind]), (kind, a)


def test_negating_a_number_that_is_not_an_integer_is_manual_work_that_waits_for_the_number_helpers(corpus_snapshot):
    decimal = parameter("@d")
    decimal.update({"declared_type": "decimal", "system_type": "decimal", "precision": 9, "scale": 2, "max_length": 5})
    sql = "CREATE PROCEDURE sales.uspTest @d decimal(9, 2) AS BEGIN\nDECLARE @n decimal(9, 2) = -@d;\nRETURN 0;\nEND"
    file = generate_text(corpus_snapshot, sql, kind="procedure", name="uspTest", parameters=[decimal])

    assert file.layer1.passed, file.layer1.failures
    assert file.file.manual_items and {m.waits_for for m in file.file.manual_items} == {("#87",)}
    assert not any("#" in m.reason for m in file.file.manual_items)


STRING_COALESCE = ("CREATE PROCEDURE sales.uspCoalesceText @a varchar(5), @b varchar(5), @r varchar(5) OUTPUT AS BEGIN\n"
                   "SET @r = COALESCE(@a, @b, 'zz');\nEND")


@pytest.mark.parametrize("values", list(itertools.product([None, "x", "yy"], repeat=2)), ids=repr)
def test_coalesce_of_strings_gives_the_first_value_that_is_not_null(corpus_snapshot, duo, values):
    parameters = [parameter("@a", "varchar", length=5), parameter("@b", "varchar", length=5),
                  parameter("@r", "varchar", length=5, direction="output")]
    file, function = procedure(corpus_snapshot, STRING_COALESCE, parameters, name="uspCoalesceText")
    arguments = dict(zip("ab", values))

    got = output_on_target(duo, function, arguments)
    expected = output_on_sql_server(duo, STRING_COALESCE, "uspCoalesceText", arguments, "varchar(5)")

    assert got == expected and file.file.manual_items == []


def test_coalesce_of_an_integer_and_a_string_converts_text_to_a_number_which_is_not_generated_yet(corpus_snapshot):
    sql = "CREATE PROCEDURE sales.uspTest @i int, @s varchar(5) AS BEGIN\nDECLARE @r int = COALESCE(@i, @s);\nRETURN @r;\nEND"
    file = generate_text(corpus_snapshot, sql, kind="procedure", name="uspTest",
                         parameters=[parameter("@i"), parameter("@s", "varchar", length=5)])

    assert file.layer1.passed, file.layer1.failures
    assert file.file.manual_items and not any("#" in m.reason for m in file.file.manual_items)
    assert "#50" not in {w for m in file.file.manual_items for w in m.waits_for}


ISNULL = "CREATE PROCEDURE sales.uspIsNull @a int, @b int AS BEGIN\nRETURN ISNULL(@a, @b);\nEND"


@pytest.mark.parametrize("values", list(itertools.product([None, 1, 2], repeat=2)), ids=repr)
def test_isnull_gives_its_first_argument_unless_that_is_null(corpus_snapshot, duo, values):
    file, function = procedure(corpus_snapshot, ISNULL, [parameter("@a"), parameter("@b")], name="uspIsNull")
    arguments = dict(zip("ab", values))

    assert outcome_on_target(duo, function, arguments) == outcome_on_sql_server(duo, ISNULL, "uspIsNull", arguments)
    assert file.file.manual_items == []


@pytest.mark.parametrize("values", [(None, 5), (None, 2147483648), (1, 2147483648), (None, -2147483649), (7, None)], ids=repr)
def test_isnull_converts_its_replacement_to_the_type_of_its_first_argument_as_sql_server_does(corpus_snapshot, duo, values):
    sql = "CREATE PROCEDURE sales.uspIsNullWide @a int, @b bigint AS BEGIN\nRETURN ISNULL(@a, @b);\nEND"
    file, function = procedure(corpus_snapshot, sql, [parameter("@a"), parameter("@b", "bigint")], name="uspIsNullWide")
    arguments = dict(zip("ab", values))

    assert outcome_on_target(duo, function, arguments) == outcome_on_sql_server(duo, sql, "uspIsNullWide", arguments)
    assert file.file.manual_items == []


# ---------------------------------------------------------------- columns (functions and views)

SPARK_TYPES = {"int": "int", "bigint": "bigint", "smallint": "smallint", "tinyint": "smallint"}
RETURNS = {"int": (4, 10), "bigint": (8, 19), "smallint": (2, 5)}


def column_function(corpus_snapshot, body: str, parameters: dict, name: str = "fnTest", returns: str = "int"):
    """A scalar function, generated as a column expression: its source, and the Python function."""
    declared = ", ".join(f"@{p} {kind}" for p, kind in parameters.items())
    sql = f"CREATE FUNCTION sales.{name} ({declared}) RETURNS {returns} AS BEGIN\nRETURN {body};\nEND"
    width, precision = RETURNS[returns]
    fact = {"system_type": returns, "max_length": width, "precision": precision, "scale": 0, "collation": None}
    file = generate_text(corpus_snapshot, sql, kind="scalar_function", name=name, schema="sales",
                         parameters=[parameter(f"@{p}", kind) for p, kind in parameters.items()], returns=fact)
    assert file.layer1.passed, file.layer1.failures
    assert file.file.manual_items == [], file.file.manual_items
    namespace: dict = {}
    exec(compile(file.text, f"{name}.py", "exec"), namespace)
    return sql, namespace[name]


def rows_on_both(duo, sql: str, name: str, function, parameters: dict, cases: list):
    """The function's value for each row of arguments, on SQL Server and on Spark: a value, or "error"; and the Spark type."""
    duo.tsql(sql.replace("sales.", "{s}."))
    ss = duo.session()
    expected, actual, dtype = [], [], None
    schema = ", ".join(f"{p} {SPARK_TYPES[kind]}" for p, kind in parameters.items())
    for case in cases:
        rows, errors = duo.run(f"SELECT {{s}}.{name}({', '.join(sql_literal(v) for v in case)}) AS r")
        expected.append("error" if errors else rows[0]["r"])
        frame = duo.spark.createDataFrame([case], schema)
        result = frame.select(function(ss, *[frame[p] for p in parameters]).alias("r"))
        dtype = result.schema["r"].dataType.simpleString()
        try:
            actual.append(result.collect()[0]["r"])
        except Exception:
            actual.append("error")
    return expected, actual, dtype


@pytest.mark.parametrize("kind", ["int", "bigint", "smallint"])
def test_a_negation_in_a_function_gives_sql_servers_value_or_fails_where_it_fails(corpus_snapshot, duo, kind):
    sql, function = column_function(corpus_snapshot, "-@a", {"a": kind}, returns=kind)

    expected, actual, dtype = rows_on_both(duo, sql, "fnTest", function, {"a": kind}, [(v,) for v in EXTREMES[kind]])

    assert actual == expected and "error" in expected and dtype == kind


def test_a_tinyint_column_is_manual_work_until_the_number_helpers_hold_its_range(corpus_snapshot):
    """Its negation is a smallint (lowering, above), but the column itself is a tinyint, which Spark holds as a SMALLINT whose
    0 to 255 range a cast cannot keep: that waits for the number helpers."""
    sql = "CREATE FUNCTION sales.fnTiny (@t tinyint) RETURNS smallint AS BEGIN\nRETURN -@t;\nEND"
    fact = {"system_type": "smallint", "max_length": 2, "precision": 5, "scale": 0, "collation": None}
    file = generate_text(corpus_snapshot, sql, kind="scalar_function", name="fnTiny", schema="sales",
                         parameters=[parameter("@t", "tinyint")], returns=fact)

    assert file.layer1.passed, file.layer1.failures
    assert file.file.manual_items and {m.waits_for for m in file.file.manual_items} == {("#87",)}


def test_coalesce_in_a_function_gives_the_first_value_that_is_not_null_in_the_widest_type(corpus_snapshot, duo):
    parameters = {"a": "int", "b": "bigint", "c": "int"}
    sql, function = column_function(corpus_snapshot, "COALESCE(@a, @b, @c)", parameters, returns="bigint")
    cases = list(itertools.product([None, 1, 2], repeat=3))

    expected, actual, dtype = rows_on_both(duo, sql, "fnTest", function, parameters, cases)

    assert actual == expected and dtype == "bigint"


@pytest.mark.parametrize("name,body", [
    ("between", "CASE WHEN @a BETWEEN @b AND @c THEN 1 ELSE 0 END"),
    ("not between", "CASE WHEN @a NOT BETWEEN @b AND @c THEN 1 ELSE 0 END"),
    ("in", "CASE WHEN @a IN (@b, @c, 7) THEN 1 ELSE 0 END"),
    ("not in", "CASE WHEN @a NOT IN (@b, @c, 7) THEN 1 ELSE 0 END"),
    ("the two together", "CASE WHEN @a BETWEEN @b AND @c THEN 1 WHEN @a NOT BETWEEN @b AND @c THEN 2 "
                         "WHEN @a IN (@b, 7) THEN 3 WHEN @a NOT IN (@b, @c, 7) THEN 4 ELSE 5 END"),
])
def test_between_and_in_in_a_function_are_three_valued(corpus_snapshot, duo, name, body):
    parameters = {"a": "int", "b": "int", "c": "int"}
    sql, function = column_function(corpus_snapshot, body, parameters)
    cases = list(itertools.product([None, 1, 2, 7], repeat=3))

    expected, actual, _ = rows_on_both(duo, sql, "fnTest", function, parameters, cases)

    assert actual == expected, name


def test_the_four_kinds_are_handled_by_the_generator():
    assert {"negate", "first_non_null", "between", "in_list"} <= generator.handled_kinds()


LAZY = "CREATE PROCEDURE sales.uspLazy @a int, @zero int AS BEGIN\nRETURN COALESCE(@a, 1 / @zero, 5);\nEND"


@pytest.mark.parametrize("values", [(1, 0), (None, 0), (None, 1), (2, 1)], ids=repr)
def test_coalesce_evaluates_an_argument_only_when_the_ones_before_it_are_null(corpus_snapshot, duo, values):
    file, function = procedure(corpus_snapshot, LAZY, [parameter("@a"), parameter("@zero")], name="uspLazy")
    arguments = dict(zip(("a", "zero"), values))

    assert outcome_on_target(duo, function, arguments) == outcome_on_sql_server(duo, LAZY, "uspLazy", arguments)
    assert file.file.manual_items == []
