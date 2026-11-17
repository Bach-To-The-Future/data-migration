"""Procedural generation, first slice (issue #50, spec section 6): a procedure, a batch or a trigger whose every
statement is generated runs as Python through session state, and one with a statement the target cannot generate yet
is reported statement by statement and raises when called, as before.

SQL Server is the oracle: each generated function runs on the sandbox's local Spark with a session state, and the same
T-SQL runs on SQL Server; what one returns the other must too. Expected values never come from the generator's own
tables. The statements generated here are the variables, assignments, `IF`, `WHILE`, `BREAK`, `CONTINUE`, `RETURN` and
`SELECT` of a procedure or batch, with the parameters and their defaults."""
import itertools
import re

import pytest

import migrt_spark as lib
from generation_helpers import generate_text
from lowering_helpers import parameter
from migrt.sources.sqlserver import statement_effects

DEFAULT = lib.DEFAULT


def procedure(corpus_snapshot, sql: str, parameters=(), name: str = "uspTest", kind: str = "procedure"):
    """The procedure generated from `sql`, and the Python function it defines."""
    generated = generate_text(corpus_snapshot, sql, kind=kind, name=name, parameters=list(parameters))
    assert generated.layer1.passed, generated.layer1.failures
    namespace: dict = {}
    exec(compile(generated.text, f"{name}.py", "exec"), namespace)
    return generated, namespace[name]


def sql_literal(value) -> str:
    if value is DEFAULT:
        return "DEFAULT"
    return "NULL" if value is None else (f"N'{value}'" if isinstance(value, str) else str(value))


def on_sql_server(duo, create: str, arguments: dict, outputs: dict[str, str] | tuple = (), name: str = "uspTest",
                  prelude: str = "", expected_errors: tuple[int, ...] = ()) -> dict:
    """Create the procedure in the scratch schema and call it with the arguments, after `prelude` (a statement that leaves
    something in the session's @@ROWCOUNT and @@ERROR). Each output parameter named in `outputs` (name -> type) is passed
    as a variable holding the argument's value. Gives the return code and each output parameter's value."""
    outputs = dict.fromkeys(outputs, "int") if not isinstance(outputs, dict) else outputs
    duo.tsql(create.replace("sales.", "{s}."))
    declarations = [f"DECLARE @o_{n} {kind} = {sql_literal(arguments[n])}" for n, kind in outputs.items()]
    passed = [f"@{n} = @o_{n} OUTPUT" if n in outputs else f"@{n} = {sql_literal(v)}" for n, v in arguments.items()]
    selected = ["@rc AS rc"] + [f"@o_{n} AS o_{n}" for n in outputs]
    rows, errors = duo.run(prelude + " DECLARE @rc int; " + "".join(d + "; " for d in declarations)
                           + f"EXEC @rc = {{s}}.{name} {', '.join(passed)}; SELECT {', '.join(selected)}")
    assert [e.number for e in errors] == list(expected_errors), errors
    return {"return_code": rows[0]["rc"], "outputs": {n: rows[0][f"o_{n}"] for n in outputs}}


def on_target(ss, function, arguments: dict, outputs: dict[str, str] | tuple = ()) -> dict:
    result = function(ss, **arguments)
    assert isinstance(result, lib.ProcedureResult)
    return {"return_code": result.return_code, "outputs": {n: result.outputs[n] for n in outputs}}


# ---------------------------------------------------------------- parameter defaults

DEFAULTS = ("CREATE PROCEDURE sales.uspTest @n int = 5, @s varchar(10) = 'x', @z int = NULL, @out int = 7 OUTPUT, "
            "@sout varchar(10) = 'q' OUTPUT AS BEGIN\n"
            "SET @out = @out + @n;\nIF @z IS NULL SET @out = @out + 1;\nSET @sout = @s;\nRETURN @out;\nEND")
DEFAULT_PARAMETERS = [parameter("@n"), parameter("@s", "varchar", length=10), parameter("@z"),
                      parameter("@out", direction="output"), parameter("@sout", "varchar", length=10, direction="output")]
OUTPUT_TYPES = {"out": "int", "sout": "varchar(10)"}


@pytest.mark.parametrize("arguments", [
    {},                                                  # every argument left out
    {"n": DEFAULT, "s": DEFAULT, "z": DEFAULT},          # DEFAULT passed explicitly
    {"n": 1}, {"n": None}, {"s": "y"}, {"s": None}, {"z": 3}, {"z": DEFAULT}, {"n": DEFAULT, "z": 2},
    {"out": 40}, {"out": None},                          # an output parameter given a value: its default does not apply
    {"s": "y", "sout": "zz"}, {"s": DEFAULT, "sout": None}, {"sout": "keep"},
], ids=repr)
def test_a_procedure_called_without_an_argument_or_with_default_behaves_as_in_sql_server(corpus_snapshot, duo, arguments):
    generated, function = procedure(corpus_snapshot, DEFAULTS, DEFAULT_PARAMETERS)
    outputs = {n: OUTPUT_TYPES[n] for n in arguments if n in OUTPUT_TYPES}
    expected = on_sql_server(duo, DEFAULTS, arguments, outputs)

    actual = on_target(duo.session(), function, arguments, outputs)

    assert actual == expected
    assert not any("default" in a for a in generated.file.assumptions)       # nothing about defaults left unapplied


def test_an_output_parameter_with_a_default_that_is_left_out_holds_its_default_in_the_result(corpus_snapshot, duo):
    generated, function = procedure(corpus_snapshot, DEFAULTS, DEFAULT_PARAMETERS)

    result = function(duo.session())

    assert result.outputs == {"out": 13, "sout": "x"} and result.return_code == 13 and result.result_sets == []
    assert "def uspTest(ss, *, n=..., s=..., z=..., out=..., sout=...) -> object:" in generated.text


def test_a_parameter_without_a_default_stays_required(corpus_snapshot, duo):
    sql = "CREATE PROCEDURE sales.uspTest @n int, @m int = 2 AS BEGIN\nRETURN @n + @m;\nEND"
    generated, function = procedure(corpus_snapshot, sql, [parameter("@n"), parameter("@m")])

    assert "def uspTest(ss, *, n, m=...) -> object:" in generated.text
    with pytest.raises(TypeError, match="n"):
        function(duo.session())
    assert function(duo.session(), n=1).return_code == 3 == on_sql_server(duo, sql, {"n": 1})["return_code"]


def test_a_scalar_function_called_with_default_gets_its_default(corpus_snapshot, duo):
    """T-SQL lets a function call pass DEFAULT but not leave the argument out."""
    sql = ("CREATE FUNCTION sales.fnSum (@a int = 3, @b int = 4) RETURNS int AS BEGIN\nRETURN @a + @b;\nEND")
    generated = generate_text(corpus_snapshot, sql, kind="scalar_function", name="fnSum", schema="sales",
                              parameters=[parameter("@a"), parameter("@b")], returns=_int())
    assert generated.layer1.passed, generated.layer1.failures
    namespace: dict = {}
    exec(compile(generated.text, "fnSum.py", "exec"), namespace)
    duo.tsql(sql.replace("sales.", "{s}."))
    ss = duo.session()

    for a, b in itertools.product([DEFAULT, 10, None], [DEFAULT, 20, None]):
        [row] = duo.tsql(f"SELECT {{s}}.fnSum({sql_literal(a)}, {sql_literal(b)}) AS r")
        column = namespace["fnSum"](ss, a, b)
        [got] = duo.spark.range(1).select(column.alias("r")).collect()
        assert got["r"] == row["r"], (a, b)
    assert not any("default" in text for text in generated.file.assumptions)
    assert "def fnSum(ss, a, b) -> Column:" in generated.text


def test_a_default_the_target_cannot_hold_makes_the_function_manual_work_and_waits_for_the_helper(corpus_snapshot):
    sql = "CREATE PROCEDURE sales.uspTest @price decimal(9, 2) = 1.50 AS BEGIN\nRETURN 0;\nEND"
    artefact_parameter = parameter("@price")
    artefact_parameter.update({"declared_type": "decimal", "system_type": "decimal", "precision": 9, "scale": 2,
                               "max_length": 5})
    generated = generate_text(corpus_snapshot, sql, kind="procedure", name="uspTest", parameters=[artefact_parameter])

    assert generated.layer1.passed, generated.layer1.failures
    assert generated.file.fails_when_called
    [item] = [m for m in generated.file.manual_items if m.waits_for == ("#87",)]
    assert "#" not in item.reason


# ---------------------------------------------------------------- variables, control flow and the return code

FLOW = ("CREATE PROCEDURE sales.uspTest @limit int, @skip int = NULL AS BEGIN\n"
        "DECLARE @i int = 0, @total int = 0, @label varchar(10);\n"
        "WHILE @i < @limit BEGIN\n"
        "  SET @i += 1;\n"
        "  IF @i = @skip CONTINUE;\n"
        "  IF @i > 7 BREAK;\n"
        "  IF @i % 2 = 0 SET @total = @total + @i; ELSE SET @total = @total - 1;\n"
        "END\n"
        "IF @total > 5 SET @label = 'big'; ELSE IF @total = 5 SET @label = 'five'; ELSE SET @label = NULL;\n"
        "IF @label IS NULL RETURN 0 - @total;\n"
        "RETURN @total * 2;\nEND")
FLOW_PARAMETERS = [parameter("@limit"), parameter("@skip")]


@pytest.mark.parametrize("arguments", [{"limit": 0}, {"limit": 3}, {"limit": 5, "skip": 2}, {"limit": 50},
                                       {"limit": 10, "skip": 4}, {"limit": None}, {"limit": 6, "skip": None}],
                         ids=repr)
def test_loops_branches_and_unknown_conditions_run_as_in_sql_server(corpus_snapshot, duo, arguments):
    generated, function = procedure(corpus_snapshot, FLOW, FLOW_PARAMETERS)
    expected = on_sql_server(duo, FLOW, arguments)

    assert on_target(duo.session(), function, arguments) == expected
    assert generated.file.manual_items == []


def test_a_declaration_run_again_in_a_loop_does_not_reset_its_variable(corpus_snapshot, duo):
    """In T-SQL a variable is declared for the whole batch: `DECLARE @seen int` in a loop leaves what the last round
    assigned, and one that is not reached leaves NULL. Only a declaration with a value assigns it."""
    sql = ("CREATE PROCEDURE sales.uspTest AS BEGIN\nDECLARE @i int = 0, @sum int = 0;\nWHILE @i < 3 BEGIN\n"
           "  DECLARE @seen int;\n  IF @seen IS NULL SET @sum = @sum + 1;\n  SET @seen = 1;\n  SET @i += 1;\nEND\n"
           "RETURN @sum;\nEND")
    generated, function = procedure(corpus_snapshot, sql)

    assert on_target(duo.session(), function, {}) == on_sql_server(duo, sql, {})


def test_a_procedure_that_states_no_return_code_returns_zero_and_its_outputs_are_what_it_assigned(corpus_snapshot, duo):
    sql = "CREATE PROCEDURE sales.uspTest @a int, @r int OUTPUT AS BEGIN\nSET @r = @a * 3;\nEND"
    generated, function = procedure(corpus_snapshot, sql, [parameter("@a"), parameter("@r", direction="output")])
    arguments = {"a": 4, "r": None}

    assert on_target(duo.session(), function, arguments, ("r",)) == on_sql_server(duo, sql, arguments, ("r",))
    assert function(duo.session(), a=4, r=None).return_code == 0


# ---------------------------------------------------------------- errors

OVERFLOW = ("CREATE PROCEDURE sales.uspTest @n int AS BEGIN\nDECLARE @m int = @n;\nSET @m = @m + 1;\nRETURN @m;\nEND")


def test_an_overflow_ends_the_procedure_with_sql_servers_error_and_sets_the_error_number(corpus_snapshot, duo):
    generated, function = procedure(corpus_snapshot, OVERFLOW, [parameter("@n")])
    duo.tsql(OVERFLOW.replace("sales.", "{s}."))
    _, errors = duo.run("DECLARE @rc int; EXEC @rc = {s}.uspTest @n = 2147483647")
    ss = duo.session()

    with pytest.raises(lib.TsqlError) as raised:
        function(ss, n=2147483647)

    assert raised.value.number == errors[0].number and ss.at_error == errors[0].number
    assert function(duo.session(), n=1).return_code == 2


def test_a_statement_that_can_fail_carries_the_caveat_that_a_failure_ends_the_function(corpus_snapshot):
    from migrt.core.differences import Catalogue
    from migrt.sources.sqlserver import differences as source_differences
    from migrt.targets.pyspark import differences as target_differences

    generated, _ = procedure(corpus_snapshot, OVERFLOW, [parameter("@n")])

    adds = [n.id for n in generated.artefact.nodes() if n.kind == "add"]
    assert adds and {generated.file.differences[i] for i in adds} == {"7.2"}
    wording = Catalogue(source_differences.half(), target_differences.half()).difference("7.2")
    assert "goes on with the next statement" in wording and "do not run" in wording


def test_a_procedure_that_reads_the_error_number_and_could_fail_needs_guarded_statements_which_are_not_generated(
        corpus_snapshot):
    sql = ("CREATE PROCEDURE sales.uspTest @n int AS BEGIN\nDECLARE @m int = @n + 1;\n"
           "IF @@ERROR <> 0 RETURN 1;\nRETURN 0;\nEND")
    generated = generate_text(corpus_snapshot, sql, kind="procedure", name="uspTest", parameters=[parameter("@n")])

    assert generated.layer1.passed, generated.layer1.failures
    assert generated.file.fails_when_called
    assert any("@@ERROR" in m.reason or "guarded" in m.reason for m in generated.file.manual_items)
    assert all(m.waits_for == ("#120",) for m in generated.file.manual_items)


# ---------------------------------------------------------------- what each statement leaves in @@ROWCOUNT and @@ERROR

# Each statement runs first in a procedure, after a statement that leaves its own values in the session, and the
# procedure returns what the session then holds: what SQL Server holds is what the generated code must. The two
# preludes leave (1, 0) and (0, 50000) in (@@ROWCOUNT, @@ERROR), so a statement that keeps a value and one that resets
# it give different answers.
PRELUDES = {"a SET": ("DECLARE @x int; SET @x = 1;", (1, 0)),
            "a RAISERROR": ("RAISERROR('x', 16, 1);", (0, 50000))}
STATEMENT_CASES = {
    "declare": "DECLARE @c int;",
    "declare with a value": "DECLARE @c int = 5;",
    "assign": "SET @pre = 4;",
    "if (false)": "IF @pre = 99 SET @pre = 3;",
    "if (true)": "IF @pre = @pre SET @pre = 3;",
    "while (never)": "WHILE @pre = 99 SET @pre = 3;",
    "while with break": "WHILE 1 = 1 BEGIN BREAK; END",
    "while with continue": "WHILE @pre < 2 BEGIN SET @pre = 2; CONTINUE; END",
    "break after an if": "WHILE 1 = 1 BEGIN IF @pre = @pre BREAK; END",
}


def read_in_procedure(statement: str, reads: str) -> str:
    return f"CREATE PROCEDURE sales.uspTest @pre int = 0 AS BEGIN\n{statement}\nRETURN {reads};\nEND"


@pytest.mark.parametrize("reads", ["@@ROWCOUNT", "@@ERROR"])
@pytest.mark.parametrize("prelude", sorted(PRELUDES))
@pytest.mark.parametrize("case", sorted(STATEMENT_CASES))
def test_each_statement_leaves_in_rowcount_and_error_what_sql_server_does(corpus_snapshot, duo, case, prelude, reads):
    sql = read_in_procedure(STATEMENT_CASES[case], reads)
    generated, function = procedure(corpus_snapshot, sql, [parameter("@pre")])
    statement, (rows, error) = PRELUDES[prelude]
    expected = on_sql_server(duo, sql, {}, prelude=statement, expected_errors=(50000,) if error else ())
    ss = duo.session()
    ss.rowcount, ss.at_error = rows, error

    assert on_target(ss, function, {}) == expected
    assert generated.file.manual_items == []


def test_a_return_leaves_one_and_a_return_of_the_row_count_reads_the_count_before_it(corpus_snapshot, duo):
    for number, body in enumerate(("SET @pre = 1;\nRETURN @@ROWCOUNT;", "IF 1 = 1 SET @pre = 2;\nRETURN @@ROWCOUNT;",
                                   "DECLARE @a int;\nRETURN @@ROWCOUNT;")):
        name = f"uspReturn{number}"
        sql = f"CREATE PROCEDURE sales.{name} AS BEGIN\nDECLARE @pre int = 0;\n{body}\nEND"
        generated, function = procedure(corpus_snapshot, sql, name=name)
        assert on_target(duo.session(), function, {}) == on_sql_server(duo, sql, {}, name=name), body


def test_every_statement_kind_the_generator_handles_has_an_effect_the_source_decided():
    handled_statements = {"declare_variable", "assign", "if", "while", "break", "continue", "return", "emit_result_set"}

    assert handled_statements <= statement_effects.statement_kinds()


# ---------------------------------------------------------------- result sets

class Tables:
    """A stand-in for `ss.spark` that serves the tables a test made, by the name the generated code reads them under."""

    def __init__(self, real, tables: dict):
        self.real, self.tables = real, tables
        self.conf = real.conf
        self.read = self

    def table(self, name: str):
        return self.tables[name]

    def __getattr__(self, name):
        return getattr(self.real, name)


def test_a_select_is_a_result_set_in_order_and_leaves_its_row_count(corpus_snapshot, duo):
    sql = ("CREATE PROCEDURE sales.uspTest @customer int AS BEGIN\n"
           "SELECT o.OrderId FROM sales.[Order] AS o WHERE o.CustomerId = @customer;\n"
           "SELECT o.OrderId AS Other FROM sales.[Order] AS o;\n"
           "RETURN @@ROWCOUNT;\nEND")
    generated, function = procedure(corpus_snapshot, sql, [parameter("@customer")])
    [table] = re.findall(r'spark\.read\.table\("([^"]+)"\)', generated.text)[:1]
    rows = duo.spark.createDataFrame([(1, 10), (2, 11), (3, 10)], "OrderId int, CustomerId int")
    ss = duo.session()
    ss.spark = Tables(ss.spark, {table: rows})

    result = function(ss, customer=10)

    assert [[r[0] for r in frame.collect()] for frame in result.result_sets] == [[1, 3], [1, 2, 3]]
    assert result.return_code == 3                        # the second select's rows: @@ROWCOUNT is the last statement's
    assert generated.file.manual_items == []


# ---------------------------------------------------------------- what is not generated yet is reported, never dropped

PARTLY = ("CREATE PROCEDURE sales.uspTest @n int AS BEGIN\nDECLARE @m int = @n;\nSET NOCOUNT ON;\n"
          "IF @m > 0 BEGIN\n  EXEC sales.uspCreateOrder @CustomerCode = 'A', @OrderId = @m OUTPUT;\nEND\nRETURN @m;\nEND")


def test_a_procedure_with_a_statement_the_target_cannot_generate_reports_that_statement_and_raises_when_called(
        corpus_snapshot, duo):
    generated = generate_text(corpus_snapshot, PARTLY, kind="procedure", name="uspTest", parameters=[parameter("@n")])

    assert generated.layer1.passed, generated.layer1.failures
    assert generated.file.fails_when_called
    kinds = sorted(n.kind for n in generated.artefact.nodes() if any(m.node == n.id for m in generated.file.manual_items))
    assert kinds == ["call_procedure"]
    assert all(m.waits_for == ("#118",) for m in generated.file.manual_items)
    assert not any("#" in m.reason for m in generated.file.manual_items)
    namespace: dict = {}
    exec(compile(generated.text, "uspTest.py", "exec"), namespace)
    with pytest.raises(RuntimeError, match="manual work"):
        namespace["uspTest"](duo.session(), n=1)


def test_a_manual_statement_leaves_none_of_the_procedures_other_code_in_the_file(corpus_snapshot):
    generated = generate_text(corpus_snapshot, PARTLY, kind="procedure", name="uspTest", parameters=[parameter("@n")])

    assert "ss.twin" not in generated.text and "ss.scope" not in generated.text


# ---------------------------------------------------------------- batches and triggers

def test_a_batch_script_runs_each_batch_as_one_function_and_gives_the_result_sets_back(corpus_snapshot, duo):
    sql = ("DECLARE @n int = 1;\nWHILE @n < 4 SET @n += 1;\nIF @n = 4 SET @n = @n * 10;\nGO\n"
           "DECLARE @m int = 2;\nSET @m = @m + 1;")
    generated = generate_text(corpus_snapshot, sql, kind="query_batch", name="load")

    assert generated.layer1.passed, generated.layer1.failures
    assert generated.file.manual_items == [] and not generated.file.fails_when_called
    assert "BATCHES = [batch_1, batch_2]" in generated.text
    namespace: dict = {}
    exec(compile(generated.text, "load.py", "exec"), namespace)
    ss = duo.session()
    results = [batch(ss) for batch in namespace["BATCHES"]]
    assert all(isinstance(r, lib.ProcedureResult) and r.result_sets == [] for r in results)
    assert (ss.rowcount, ss.at_error) == (1, 0)           # the last statement of the last batch was a SET


def test_a_trigger_of_generated_statements_takes_the_changed_rows_and_returns_nothing(corpus_snapshot, duo):
    sql = ("CREATE TRIGGER sales.trTest ON sales.[Order] AFTER INSERT AS BEGIN\nDECLARE @n int = 1;\n"
           "IF @n = 1 SET @n = 2;\nEND")
    generated = generate_text(corpus_snapshot, sql, kind="trigger", name="trTest", table="RetailSample.sales.Order",
                              timing="AFTER", events=("INSERT",))

    assert generated.layer1.passed, generated.layer1.failures
    assert "def trTest(ss, inserted, deleted) -> None:" in generated.text and generated.file.manual_items == []
    namespace: dict = {}
    exec(compile(generated.text, "trTest.py", "exec"), namespace)
    assert namespace["trTest"](duo.session(), None, None) is None


def test_a_trigger_that_reads_the_changed_rows_is_still_reported_statement_by_statement(corpus_snapshot):
    sql = ("CREATE TRIGGER sales.trTest ON sales.[Order] AFTER INSERT AS BEGIN\n"
           "DECLARE @n int;\nSELECT @n = COUNT(*) FROM inserted AS i;\nEND")
    generated = generate_text(corpus_snapshot, sql, kind="trigger", name="trTest", table="RetailSample.sales.Order",
                              timing="AFTER", events=("INSERT",))

    assert generated.layer1.passed, generated.layer1.failures
    assert generated.file.fails_when_called and {m.waits_for for m in generated.file.manual_items} == {("#120",)}


def _int():
    return {"system_type": "int", "max_length": 4, "precision": 10, "scale": 0, "collation": None}


def test_reading_the_transaction_state_is_manual_work_that_waits_for_doomed_transactions(corpus_snapshot):
    sql = "CREATE PROCEDURE sales.uspTest AS BEGIN\nDECLARE @s int = XACT_STATE();\nRETURN @s;\nEND"
    generated = generate_text(corpus_snapshot, sql, kind="procedure", name="uspTest")

    assert generated.layer1.passed, generated.layer1.failures
    [item] = [m for m in generated.file.manual_items if "state of the transaction" in m.reason]
    assert item.waits_for == ("#113",) and "#" not in item.reason
