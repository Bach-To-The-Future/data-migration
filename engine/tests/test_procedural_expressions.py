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


# ---------------------------------------------------------------- columns (functions and views)

def column_function(corpus_snapshot, body: str, parameters: list, name: str = "fnTest"):
    """A scalar function, generated as a column expression: its source, and the Python function."""
    sql = f"CREATE FUNCTION sales.{name} ({', '.join(f'@{p} int' for p in parameters)}) RETURNS int AS BEGIN\nRETURN {body};\nEND"
    returns = {"system_type": "int", "max_length": 4, "precision": 10, "scale": 0, "collation": None}
    file = generate_text(corpus_snapshot, sql, kind="scalar_function", name=name, schema="sales",
                         parameters=[parameter(f"@{p}") for p in parameters], returns=returns)
    assert file.layer1.passed, file.layer1.failures
    assert file.file.manual_items == [], file.file.manual_items
    namespace: dict = {}
    exec(compile(file.text, f"{name}.py", "exec"), namespace)
    return sql, namespace[name]


def rows_on_both(duo, sql: str, name: str, function, parameters: list, cases: list):
    """The function's value for each row of arguments, on SQL Server and on Spark: a value, or "error"."""
    duo.tsql(sql.replace("sales.", "{s}."))
    ss = duo.session()
    expected, actual = [], []
    for case in cases:
        rows, errors = duo.run(f"SELECT {{s}}.{name}({', '.join(sql_literal(v) for v in case)}) AS r")
        expected.append("error" if errors else rows[0]["r"])
        frame = duo.spark.createDataFrame([case], ", ".join(f"{p} int" for p in parameters))
        columns = [frame[p] for p in parameters]
        try:
            actual.append(frame.select(function(ss, *columns).alias("r")).collect()[0]["r"])
        except Exception:
            actual.append("error")
    return expected, actual


def test_a_negation_in_a_function_gives_sql_servers_value_or_fails_where_it_fails(corpus_snapshot, duo):
    sql, function = column_function(corpus_snapshot, "-@a", ["a"])

    expected, actual = rows_on_both(duo, sql, "fnTest", function, ["a"], [(v,) for v in INT_EDGES])

    assert actual == expected and "error" in expected


def test_coalesce_in_a_function_gives_the_first_value_that_is_not_null(corpus_snapshot, duo):
    sql, function = column_function(corpus_snapshot, "COALESCE(@a, @b, @c)", ["a", "b", "c"])
    cases = list(itertools.product([None, 1, 2], repeat=3))

    expected, actual = rows_on_both(duo, sql, "fnTest", function, ["a", "b", "c"], cases)

    assert actual == expected


def test_between_and_in_in_a_function_are_three_valued(corpus_snapshot, duo):
    body = ("CASE WHEN @a BETWEEN @b AND @c THEN 1 WHEN @a NOT BETWEEN @b AND @c THEN 2 "
            "WHEN @a IN (@b, 7) THEN 3 WHEN @a NOT IN (@b, @c, 7) THEN 4 ELSE 5 END")
    sql, function = column_function(corpus_snapshot, body, ["a", "b", "c"])
    cases = list(itertools.product([None, 1, 2, 7], repeat=3))

    expected, actual = rows_on_both(duo, sql, "fnTest", function, ["a", "b", "c"], cases)

    assert actual == expected


def test_the_four_kinds_are_handled_by_the_generator():
    assert {"negate", "first_non_null", "between", "in_list"} <= generator.handled_kinds()
