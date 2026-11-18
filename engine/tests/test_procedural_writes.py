"""Procedural generation, writes and `EXEC` (issue #118, spec section 6): `INSERT`, `UPDATE` and `DELETE` run through
session state's write wrappers, and `EXEC` calls the callee's function inside `ss.scope(name, procedure_call=True)`.

SQL Server is the oracle: each generated procedure runs on the sandbox's local Spark against scratch tables, the same
T-SQL runs on SQL Server against the same tables, and the tables, the return code, the output parameters, `@@ROWCOUNT`
and `@@ERROR` are compared. Expected values never come from the generator's own tables.

The generated code names its tables as the project does (the Unity Catalog name of `RetailSample.sales.Inventory`); a
test binds each of those names to the scratch table of the same last part, which is the only edit made to the code."""
import re
import sys
import types

import pytest

import migrt_spark as lib
from generation_helpers import generate_text
from lowering_helpers import parameter, synthetic_module
from support_harness import C, assert_same_error
from test_lowering_writes import described
from test_procedural_generation import on_sql_server, on_target, sql_literal

NAME = re.compile(r'"(`[^`"]+`\.`[^`"]+`\.`[^`"]+`)"')
INVENTORY = ["ProductId", "OnHand", "ReorderLevel"]
STOCK = [(1, 10, 5), (2, 3, 5), (3, 0, 1), (4, 7, 2)]


def world(duo) -> None:
    """`sales.Inventory` and `audit.ErrorLog` as scratch tables on both sides, the first with rows."""
    duo.table("Inventory", [C("ProductId", "int", nullable=False), C("OnHand", "int", nullable=False),
                            C("ReorderLevel", "int", nullable=False)],
              primary_key=("ProductId",), checks=(lib.Check("CK_Inventory_OnHand", "OnHand >= 0", ("OnHand",)),))
    duo.table("ErrorLog", [C("ErrorLogId", "int", nullable=False, identity=(1, 1)),
                           C("ProcName", "nvarchar(128)", nullable=False), C("ErrorNumber", "int", nullable=False),
                           C("ErrorMessage", "nvarchar(4000)", nullable=False)], primary_key=("ErrorLogId",))
    rows = ", ".join(str(r) for r in STOCK)
    duo.tsql(f"INSERT {{s}}.Inventory VALUES {rows}")
    duo.spark.sql(f"INSERT INTO {duo.target('Inventory')} VALUES {rows}")


def bound(duo, text: str) -> str:
    """The generated code with each project table name replaced by the scratch table of the same name."""
    def scratch(found: re.Match) -> str:
        last = found.group(1).split(".")[-1].strip("`").lower()
        return f'"{duo.target(next(n for n in duo.specs if n.lower() == last))}"'
    return NAME.sub(scratch, text)


def server(sql: str) -> str:
    """T-SQL for the scratch schema: `audit.` tables live beside the `sales.` ones there."""
    return sql.replace("audit.", "sales.")


def generated_procedure(corpus_snapshot, duo, sql: str, parameters=(), name: str = "uspTest", **more):
    """The procedure generated from `sql`, and the Python function it defines once its tables are the scratch ones."""
    generated = generate_text(corpus_snapshot, sql, kind="procedure", name=name, parameters=list(parameters), **more)
    assert generated.layer1.passed, generated.layer1.failures
    namespace: dict = {}
    exec(compile(bound(duo, generated.text), f"{name}.py", "exec"), namespace)
    return generated, namespace[name]


def proc(body: str, params: str = "", name: str = "uspTest") -> str:
    return f"CREATE PROCEDURE sales.{name} {params} AS BEGIN\n{body}\nEND"


def assert_inventory_same(duo) -> list[tuple]:
    return duo.assert_same("Inventory", INVENTORY)


# ---------------------------------------------------------------- INSERT

INSERTS = {
    "values": "INSERT sales.Inventory (ProductId, OnHand, ReorderLevel) VALUES (10, 1, 2), (11, @n, 3);",
    "values without a column list": "INSERT sales.Inventory VALUES (10, 1, 2);",
    "values from variables": "DECLARE @p int = @n + 20;\nINSERT sales.Inventory (ProductId, OnHand, ReorderLevel) "
                             "VALUES (@p, @n, @n);",
    "a query": "INSERT sales.Inventory (ProductId, OnHand, ReorderLevel) "
               "SELECT i.ProductId + 100, i.OnHand, i.ReorderLevel FROM sales.Inventory AS i WHERE i.OnHand > @n;",
    "a query that finds nothing": "INSERT sales.Inventory (ProductId, OnHand, ReorderLevel) "
                                  "SELECT i.ProductId + 100, i.OnHand, i.ReorderLevel FROM sales.Inventory AS i "
                                  "WHERE i.OnHand > 1000;",
    "a query that names its columns in another order": "INSERT sales.Inventory (OnHand, ProductId, ReorderLevel) "
                                                       "SELECT i.OnHand, i.ProductId + 100, i.ReorderLevel "
                                                       "FROM sales.Inventory AS i;",
}


@pytest.mark.parametrize("case", sorted(INSERTS))
@pytest.mark.parametrize("n", [0, 4])
def test_an_insert_writes_the_rows_and_leaves_the_row_count_sql_server_does(corpus_snapshot, duo, case, n):
    world(duo)
    sql = proc(f"{INSERTS[case]}\nRETURN @@ROWCOUNT;", "@n int")
    generated, function = generated_procedure(corpus_snapshot, duo, sql, [parameter("@n")])
    expected = on_sql_server(duo, sql, {"n": n})

    actual = on_target(duo.session(), function, {"n": n})

    assert actual == expected
    assert_inventory_same(duo)
    assert generated.file.manual_items == []
    assert "ss.insert" in generated.text


def test_an_insert_into_a_table_with_an_identity_column_gets_the_identity_values_sql_server_gives(corpus_snapshot, duo):
    world(duo)
    sql = proc("INSERT audit.ErrorLog (ProcName, ErrorNumber, ErrorMessage) VALUES (N'uspA', 1, N'one'), "
               "(N'uspB', 2, N'two');\nINSERT audit.ErrorLog (ProcName, ErrorNumber, ErrorMessage) "
               "VALUES (N'uspC', 3, N'three');")
    generated, function = generated_procedure(corpus_snapshot, duo, sql)
    duo.tsql(server(sql).replace("sales.", "{s}."))
    duo.tsql("EXEC {s}.uspTest")

    function(duo.session())

    assert duo.assert_same("ErrorLog", ["ErrorLogId", "ProcName", "ErrorNumber", "ErrorMessage"]) == [
        (1, "uspA", 1, "one"), (2, "uspB", 2, "two"), (3, "uspC", 3, "three")]
    assert generated.file.manual_items == []


# ---------------------------------------------------------------- UPDATE and DELETE

UPDATES = {
    "with a condition": "UPDATE sales.Inventory SET OnHand = OnHand + @n WHERE ProductId = @n;",
    "several columns": "UPDATE sales.Inventory SET OnHand = ReorderLevel, ReorderLevel = OnHand WHERE OnHand > @n;",
    "of every row": "UPDATE sales.Inventory SET ReorderLevel = @n;",
    "that matches nothing": "UPDATE sales.Inventory SET OnHand = 1 WHERE ProductId = 999;",
    "through a condition that is unknown": "UPDATE sales.Inventory SET OnHand = 1 WHERE ProductId = NULL;",
    "with a column alias": "UPDATE i SET i.OnHand = i.OnHand + 1 FROM sales.Inventory AS i WHERE i.ProductId <= @n;",
}
DELETES = {
    "with a condition": "DELETE sales.Inventory WHERE OnHand < @n;",
    "of every row": "DELETE sales.Inventory;",
    "that matches nothing": "DELETE sales.Inventory WHERE ProductId = 999;",
    "with a column alias": "DELETE i FROM sales.Inventory AS i WHERE i.ProductId <= @n;",
}


@pytest.mark.parametrize("case", sorted(UPDATES))
@pytest.mark.parametrize("n", [1, 3])
def test_an_update_changes_the_rows_and_leaves_the_row_count_sql_server_does(corpus_snapshot, duo, case, n):
    world(duo)
    sql = proc(f"{UPDATES[case]}\nRETURN @@ROWCOUNT;", "@n int")
    generated, function = generated_procedure(corpus_snapshot, duo, sql, [parameter("@n")])
    expected = on_sql_server(duo, sql, {"n": n})

    actual = on_target(duo.session(), function, {"n": n})

    assert actual == expected
    assert_inventory_same(duo)
    assert generated.file.manual_items == []
    assert "ss.update" in generated.text


@pytest.mark.parametrize("case", sorted(DELETES))
@pytest.mark.parametrize("n", [1, 5])
def test_a_delete_removes_the_rows_and_leaves_the_row_count_sql_server_does(corpus_snapshot, duo, case, n):
    world(duo)
    sql = proc(f"{DELETES[case]}\nRETURN @@ROWCOUNT;", "@n int")
    generated, function = generated_procedure(corpus_snapshot, duo, sql, [parameter("@n")])
    expected = on_sql_server(duo, sql, {"n": n})

    actual = on_target(duo.session(), function, {"n": n})

    assert actual == expected
    assert_inventory_same(duo)
    assert generated.file.manual_items == []
    assert "ss.delete" in generated.text


# ---------------------------------------------------------------- what a write leaves in @@ROWCOUNT and @@ERROR

# Each write runs after a statement that leaves its own values in the session, as a batch on SQL Server, and the session
# holds what SQL Server then holds. (A procedure that reads @@ERROR and has a statement that could fail needs guarded
# statements, which are not generated, so the values are read from the session rather than returned.)
PRELUDES = {"a SET": ("DECLARE @x int; SET @x = 1;", (1, 0)),
            "a RAISERROR": ("RAISERROR('x', 16, 1);", (0, 50000))}
WRITES = {
    "insert": "INSERT sales.Inventory (ProductId, OnHand, ReorderLevel) VALUES (20, 1, 1), (21, 1, 1);",
    "insert of no rows": "INSERT sales.Inventory (ProductId, OnHand, ReorderLevel) "
                         "SELECT i.ProductId + 100, i.OnHand, i.ReorderLevel FROM sales.Inventory AS i WHERE i.OnHand > 99;",
    "update": "UPDATE sales.Inventory SET OnHand = OnHand + 1;",
    "update of no rows": "UPDATE sales.Inventory SET OnHand = 1 WHERE ProductId = 999;",
    "delete": "DELETE sales.Inventory WHERE OnHand < 5;",
    "delete of no rows": "DELETE sales.Inventory WHERE ProductId = 999;",
}


@pytest.mark.parametrize("prelude", sorted(PRELUDES))
@pytest.mark.parametrize("case", sorted(WRITES))
def test_each_write_leaves_in_rowcount_and_error_what_sql_server_does(corpus_snapshot, duo, case, prelude):
    world(duo)
    statement, (rows, error) = PRELUDES[prelude]
    generated, function = generated_procedure(corpus_snapshot, duo, proc(WRITES[case]))
    [expected] = duo.run(f"{statement} {WRITES[case].replace('sales.', '{s}.')} SELECT @@ROWCOUNT AS rc, @@ERROR AS err")[0]
    ss = duo.session()
    ss.rowcount, ss.at_error = rows, error

    function(ss)

    assert (ss.rowcount, ss.at_error) == (expected["rc"], expected["err"])
    assert_inventory_same(duo)
    assert generated.file.manual_items == []


# ---------------------------------------------------------------- failures

FAILING = {
    "a check constraint": ("UPDATE sales.Inventory SET OnHand = OnHand - 100;", {}),
    "a primary key": ("INSERT sales.Inventory (ProductId, OnHand, ReorderLevel) VALUES (1, 1, 1);", {}),
    "a NULL in a column that does not allow it": ("INSERT sales.Inventory (ProductId, OnHand, ReorderLevel) "
                                                  "VALUES (30, NULL, 1);", {}),
    "a NULL given by a variable": ("INSERT sales.Inventory (ProductId, OnHand, ReorderLevel) VALUES (31, @n, 1);",
                                   {"n": None}),
    "an overflow in the values": ("INSERT sales.Inventory (ProductId, OnHand, ReorderLevel) VALUES (32, @n + 1, 1);",
                                  {"n": 2147483647}),
}


@pytest.mark.parametrize("case", sorted(FAILING))
def test_a_write_that_fails_raises_the_error_sql_server_raises_and_changes_nothing(corpus_snapshot, duo, case):
    world(duo)
    statement, arguments = FAILING[case]
    sql = proc(statement, "@n int = 0")
    generated, function = generated_procedure(corpus_snapshot, duo, sql, [parameter("@n")])
    arguments = {"n": 0, **arguments}
    duo.tsql(server(sql).replace("sales.", "{s}."))
    ss = duo.session()

    assert_same_error(duo, f"EXEC {{s}}.uspTest @n = {sql_literal(arguments['n'])}", lambda: function(ss, **arguments))

    assert_inventory_same(duo)
    assert generated.file.manual_items == []


def test_a_write_carries_the_caveat_that_a_failure_ends_the_function(corpus_snapshot, duo):
    world(duo)
    generated, _ = generated_procedure(corpus_snapshot, duo, proc("DELETE sales.Inventory;"))

    assert list(generated.file.differences.values()) == ["7.2"]


# ---------------------------------------------------------------- the table the write names

def test_a_write_names_the_table_as_the_project_does_and_the_report_says_the_session_must_describe_it(corpus_snapshot, duo):
    world(duo)
    generated = generate_text(corpus_snapshot, proc("DELETE sales.Inventory;"), kind="procedure", name="uspTest")

    [name] = set(NAME.findall(generated.text))
    assert f'ss.delete("{name}")' in generated.text
    assert any("Inventory" in a and "session" in a for a in generated.file.assumptions)


# ---------------------------------------------------------------- what is not generated is reported with what it waits for

THREE_INTEGERS = [{"name": n, "declared_type": "int", "system_type": "int", "max_length": 4, "precision": 10, "scale": 0,
                   "nullable": False} for n in INVENTORY]


def write_items(generated, kinds=("insert_rows", "update_rows", "delete_rows", "merge_rows")):
    """The manual items that replace a write statement."""
    nodes = {n.id: n for n in generated.artefact.nodes()}
    return [i for i in generated.file.manual_items if nodes[i.replaces].kind in kinds]


@pytest.mark.parametrize("sql, waits_for", [
    ("DECLARE @t TABLE (ProductId int, OnHand int, ReorderLevel int);\nINSERT @t VALUES (1, 1, 1);", "#120"),
    ("CREATE TABLE #t (ProductId int, OnHand int, ReorderLevel int);\nINSERT #t VALUES (1, 1, 1);", "#120"),
    ("DECLARE @t TABLE (ProductId int, OnHand int, ReorderLevel int);\nUPDATE @t SET OnHand = 1;", "#120"),
    ("CREATE TABLE #t (ProductId int, OnHand int, ReorderLevel int);\nDELETE #t;", "#120"),
    ("INSERT sales.Inventory (ProductId, OnHand, ReorderLevel) OUTPUT inserted.ProductId VALUES (40, 1, 1);", "#121"),
    ("UPDATE sales.Inventory SET OnHand = 1 OUTPUT deleted.OnHand;", "#121"),
    ("INSERT sales.Inventory (ProductId, OnHand, ReorderLevel) EXEC sales.uspGetOrdersByCustomer 'A';", "#121"),
    ("MERGE sales.Inventory AS t USING (SELECT 1 AS p) AS s ON t.ProductId = s.p WHEN MATCHED THEN DELETE;", "#121"),
], ids=["insert into a table variable", "insert into a temp table", "update of a table variable", "delete from a temp table",
        "insert OUTPUT", "update OUTPUT", "insert exec", "merge"])
def test_a_write_form_not_generated_is_manual_work_that_waits_for_its_ticket(corpus_snapshot, sql, waits_for):
    snapshot = described(corpus_snapshot, shape=THREE_INTEGERS)

    generated = generate_text(snapshot, proc(sql), kind="procedure", name="uspTest")

    [item] = write_items(generated)
    assert waits_for in item.waits_for
    assert generated.file.fails_when_called


def test_a_list_of_values_as_the_row_source_of_a_query_still_waits_for_the_set_like_row_sources(corpus_snapshot):
    sql = proc("SELECT v.a FROM (VALUES (1), (2)) AS v(a);")

    generated = generate_text(corpus_snapshot, sql, kind="procedure", name="uspTest")

    [item] = generated.file.manual_items
    assert item.waits_for == ("#129",)


def test_an_update_or_delete_from_other_rows_than_the_table_is_manual_work_with_a_reason(corpus_snapshot):
    sql = proc("UPDATE i SET i.OnHand = o.OnHand FROM sales.Inventory AS i JOIN sales.Inventory AS o "
               "ON o.ProductId = i.ProductId + 1;")

    generated = generate_text(corpus_snapshot, sql, kind="procedure", name="uspTest")

    [item] = write_items(generated)
    assert "join" in item.reason.lower() and generated.file.fails_when_called


# ---------------------------------------------------------------- EXEC

CALLEE = ("CREATE PROCEDURE sales.uspCallee @a int, @b int = 10, @c int = 20 OUTPUT AS BEGIN\n"
          "SET @c = @a + @b + ISNULL(@c, 0);\nRETURN @c * 2;\nEND")
CALLEE_PARAMETERS = [parameter("@a"), parameter("@b", default="10"), parameter("@c", direction="output", default="20")]
CALLER_PARAMETERS = [parameter("@x"), parameter("@y", direction="output")]


def generated_pair(corpus_snapshot, duo, callee_sql: str, callee_parameters, caller_sql: str, caller_parameters,
                   callee_name: str = "uspCallee", caller_name: str = "uspCaller"):
    """Both procedures generated into one project, the callee defined as the module the caller imports, and the caller's
    function."""
    callee = generate_text(corpus_snapshot, callee_sql, kind="procedure", name=callee_name,
                           parameters=list(callee_parameters))
    assert callee.layer1.passed, callee.layer1.failures
    identity = callee.artefact.identity
    snapshot = synthetic_module(corpus_snapshot, callee_sql, kind="procedure", name=callee_name,
                                parameters=list(callee_parameters))[0].snapshot
    caller = generate_text(snapshot, caller_sql, kind="procedure", name=caller_name, parameters=list(caller_parameters),
                           generated_in_project={identity: "procedure"})
    assert caller.layer1.passed, caller.layer1.failures
    module = types.ModuleType(callee.file.relative_path.removesuffix(".py").replace("/", "."))
    exec(compile(bound(duo, callee.text), f"{callee_name}.py", "exec"), module.__dict__)
    namespace: dict = {}
    return callee, caller, module, namespace


def install(monkeypatch, module: types.ModuleType) -> None:
    """Make `module` importable under its dotted name, as the delivered project's package is."""
    parts = module.__name__.split(".")
    for i in range(1, len(parts)):
        package = types.ModuleType(".".join(parts[:i]))
        package.__path__ = []                                  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, module.__name__, module)


def define(duo, monkeypatch, corpus_snapshot, callee_sql, callee_parameters, caller_sql, caller_parameters, *,
           by_hand=None, **names):
    """The caller's function, with the callee installed as the module it imports. `by_hand` is a function to stand in for the
    callee's own, for a behaviour the generator cannot write yet."""
    callee, caller, module, namespace = generated_pair(corpus_snapshot, duo, callee_sql, callee_parameters, caller_sql,
                                                       caller_parameters, **names)
    if by_hand is not None:
        setattr(module, names.get("callee_name", "uspCallee"), by_hand)
    install(monkeypatch, module)
    exec(compile(bound(duo, caller.text), "caller.py", "exec"), namespace)
    return callee, caller, namespace[names.get("caller_name", "uspCaller")]


CALLS = {
    "arguments by position": "EXEC @rc = sales.uspCallee @x, 2, @y OUTPUT;",
    "arguments by name": "EXEC @rc = sales.uspCallee @c = @y OUTPUT, @a = @x, @b = 5;",
    "an argument left out": "EXEC @rc = sales.uspCallee @a = @x, @c = @y OUTPUT;",
    "every defaulted argument left out": "EXEC @rc = sales.uspCallee @a = @x;",
    "DEFAULT by position": "EXEC @rc = sales.uspCallee @x, DEFAULT, DEFAULT;",
    "DEFAULT for an output parameter": "EXEC @rc = sales.uspCallee @a = @x, @b = DEFAULT, @c = DEFAULT;",
    "the return code ignored": "EXEC sales.uspCallee @x, 3, @y OUTPUT;",
    "an output parameter passed as an input": "EXEC @rc = sales.uspCallee @x, 3, @y;",
    "a null argument": "EXEC @rc = sales.uspCallee @a = NULL, @c = @y OUTPUT;",
}


@pytest.mark.parametrize("case", sorted(CALLS))
@pytest.mark.parametrize("y", [None, 4])
def test_a_call_passes_arguments_uses_defaults_and_gives_back_the_outputs_and_the_return_code(
        corpus_snapshot, duo, monkeypatch, case, y):
    caller_sql = ("CREATE PROCEDURE sales.uspCaller @x int, @y int OUTPUT AS BEGIN\nDECLARE @rc int = -1;\n"
                  f"{CALLS[case]}\nSET @y = ISNULL(@y, -2) * 1;\nRETURN @rc;\nEND")
    duo.tsql(CALLEE.replace("sales.", "{s}."))
    callee, caller, function = define(duo, monkeypatch, corpus_snapshot, CALLEE, CALLEE_PARAMETERS, caller_sql,
                                      CALLER_PARAMETERS)
    arguments = {"x": 1, "y": y}
    expected = on_sql_server(duo, caller_sql, arguments, {"y": "int"}, name="uspCaller")

    actual = on_target(duo.session(), function, arguments, {"y": "int"})

    assert actual == expected
    assert caller.file.manual_items == [] and callee.file.manual_items == []
    assert "with ss.scope('uspCallee', procedure_call=True):" in caller.text


def test_a_callee_with_a_parameter_whose_python_name_is_changed_is_called_by_that_name(corpus_snapshot, duo, monkeypatch):
    callee_sql = ("CREATE PROCEDURE sales.uspCallee @class int, @ss int = 1, @in int = 2 OUTPUT AS BEGIN\n"
                  "SET @in = @class + @ss + @in;\nRETURN @in;\nEND")
    parameters = [parameter("@class"), parameter("@ss", default="1"), parameter("@in", direction="output", default="2")]
    caller_sql = ("CREATE PROCEDURE sales.uspCaller @y int OUTPUT AS BEGIN\nDECLARE @rc int;\n"
                  "EXEC @rc = sales.uspCallee @class = 5, @in = @y OUTPUT;\nRETURN @rc;\nEND")
    duo.tsql(callee_sql.replace("sales.", "{s}."))
    callee, caller, function = define(duo, monkeypatch, corpus_snapshot, callee_sql, parameters, caller_sql,
                                      [parameter("@y", direction="output")])
    expected = on_sql_server(duo, caller_sql, {"y": 7}, {"y": "int"}, name="uspCaller")

    actual = on_target(duo.session(), function, {"y": 7}, {"y": "int"})

    assert actual == expected and actual["outputs"]["y"] == 13
    assert caller.file.manual_items == [] and callee.file.manual_items == []


def test_a_parameter_left_out_that_has_no_default_is_manual_work_with_a_reason(corpus_snapshot, duo, monkeypatch):
    caller_sql = "CREATE PROCEDURE sales.uspCaller AS BEGIN\nEXEC sales.uspCallee @b = 1;\nEND"
    _, caller, _, _ = generated_pair(corpus_snapshot, duo, CALLEE, CALLEE_PARAMETERS, caller_sql, [])

    [item] = caller.file.manual_items
    assert "@a" in item.reason and "default" in item.reason and item.waits_for == ()


def test_a_callee_that_is_not_generated_in_the_project_is_manual_work_with_a_reason(corpus_snapshot, duo):
    sql = ("CREATE PROCEDURE sales.uspTest @n int AS BEGIN\nEXEC sales.uspCreateOrder @CustomerCode = 'A', "
           "@OrderId = @n OUTPUT;\nEND")
    generated = generate_text(corpus_snapshot, sql, kind="procedure", name="uspTest",
                              parameters=[parameter("@n")])

    [item] = generated.file.manual_items
    assert "uspCreateOrder" in item.reason and "not generated" in item.reason
    assert item.waits_for == () and generated.file.fails_when_called


def test_a_call_in_a_trigger_is_manual_work_because_the_result_sets_of_the_callee_would_go_nowhere(corpus_snapshot, duo):
    callee_sql = "CREATE PROCEDURE sales.uspCallee AS BEGIN\nRETURN 1;\nEND"
    snapshot = synthetic_module(corpus_snapshot, callee_sql, kind="procedure", name="uspCallee")[0].snapshot
    sql = ("CREATE TRIGGER sales.trTest ON sales.[Order] AFTER UPDATE AS BEGIN\nEXEC sales.uspCallee;\nEND")

    generated = generate_text(snapshot, sql, kind="trigger", name="trTest",
                              generated_in_project={"RetailSample.sales.uspCallee": "procedure"})

    [item] = generated.file.manual_items
    assert "trigger" in item.reason and item.waits_for == ("#122",)


def test_the_result_sets_of_a_callee_go_to_the_callers_caller_in_order(corpus_snapshot, duo, monkeypatch):
    world(duo)
    callee_sql = ("CREATE PROCEDURE sales.uspCallee @n int AS BEGIN\n"
                  "SELECT i.ProductId FROM sales.Inventory AS i WHERE i.OnHand > @n;\n"
                  "SELECT i.OnHand AS Other FROM sales.Inventory AS i;\nEND")
    caller_sql = ("CREATE PROCEDURE sales.uspCaller AS BEGIN\nSELECT i.ReorderLevel FROM sales.Inventory AS i;\n"
                  "EXEC sales.uspCallee @n = 4;\nSELECT i.ProductId FROM sales.Inventory AS i WHERE i.OnHand = 0;\nEND")
    callee, caller, function = define(duo, monkeypatch, corpus_snapshot, callee_sql, [parameter("@n")], caller_sql, [])
    selects = ["SELECT i.ReorderLevel FROM {s}.Inventory AS i", "SELECT i.ProductId FROM {s}.Inventory AS i WHERE i.OnHand > 4",
               "SELECT i.OnHand FROM {s}.Inventory AS i", "SELECT i.ProductId FROM {s}.Inventory AS i WHERE i.OnHand = 0"]
    expected = [sorted(next(iter(r.values())) for r in duo.tsql(select)) for select in selects]

    result = function(duo.session())

    assert [sorted(r[0] for r in frame.collect()) for frame in result.result_sets] == expected
    assert caller.file.manual_items == [] and callee.file.manual_items == []


def test_rowcount_after_a_call_is_what_the_callees_last_statement_left(corpus_snapshot, duo, monkeypatch):
    world(duo)
    callee_sql = "CREATE PROCEDURE sales.uspCallee AS BEGIN\nUPDATE sales.Inventory SET OnHand = OnHand + 1 WHERE OnHand > 2;\nEND"
    caller_sql = ("CREATE PROCEDURE sales.uspCaller AS BEGIN\nDELETE sales.Inventory WHERE ProductId = 999;\n"
                  "EXEC sales.uspCallee;\nRETURN @@ROWCOUNT;\nEND")
    callee, caller, function = define(duo, monkeypatch, corpus_snapshot, callee_sql, [], caller_sql, [])
    duo.tsql(callee_sql.replace("sales.", "{s}."))
    expected = on_sql_server(duo, caller_sql, {}, name="uspCaller")

    actual = on_target(duo.session(), function, {})

    assert actual == expected and actual["return_code"] == 3
    assert_inventory_same(duo)


def test_the_error_after_a_call_is_reset(corpus_snapshot, duo, monkeypatch):
    callee_sql = "CREATE PROCEDURE sales.uspCallee AS BEGIN\nDECLARE @a int = 1;\nEND"
    caller_sql = "CREATE PROCEDURE sales.uspCaller AS BEGIN\nEXEC sales.uspCallee;\nEND"
    _, _, function = define(duo, monkeypatch, corpus_snapshot, callee_sql, [], caller_sql, [])
    duo.tsql(callee_sql.replace("sales.", "{s}."))
    [expected] = duo.run("RAISERROR('x', 16, 1); EXEC {s}.uspCallee; SELECT @@ERROR AS err")[0]
    ss = duo.session()
    ss.at_error = 50000

    function(ss)

    assert ss.at_error == expected["err"] == 0


def test_a_call_runs_inside_a_scope_that_raises_error_266_for_a_callee_that_leaves_a_transaction_open(
        corpus_snapshot, duo, monkeypatch):
    """A callee that opens a transaction and does not close it. Opening one is not generated yet, so the callee here is
    the function a person would write by hand; the generated caller's scope is what raises."""
    callee_sql = "CREATE PROCEDURE sales.uspCallee AS BEGIN\nDECLARE @a int = 1;\nEND"
    caller_sql = "CREATE PROCEDURE sales.uspCaller AS BEGIN\nEXEC sales.uspCallee;\nEND"

    def opens_and_forgets(ss) -> lib.ProcedureResult:
        ss.begin_transaction()
        return ss.result(0, {}, [])

    callee, caller, function = define(duo, monkeypatch, corpus_snapshot, callee_sql, [], caller_sql, [],
                                      by_hand=opens_and_forgets)
    duo.tsql("CREATE PROCEDURE {s}.uspOpens AS BEGIN BEGIN TRANSACTION; END")
    duo.tsql("CREATE PROCEDURE {s}.uspCalls AS BEGIN EXEC {s}.uspOpens; END")
    ss = duo.session()

    assert_same_error(duo, "EXEC {s}.uspCalls", lambda: function(ss))
    assert "with ss.scope('uspCallee', procedure_call=True):" in caller.text
    duo.tsql("IF @@TRANCOUNT > 0 ROLLBACK")


def test_a_balanced_callee_does_not_raise_266(corpus_snapshot, duo, monkeypatch):
    callee_sql = "CREATE PROCEDURE sales.uspCallee AS BEGIN\nDECLARE @a int = 1;\nEND"
    caller_sql = "CREATE PROCEDURE sales.uspCaller AS BEGIN\nEXEC sales.uspCallee;\nRETURN 3;\nEND"
    _, _, function = define(duo, monkeypatch, corpus_snapshot, callee_sql, [], caller_sql, [])

    assert function(duo.session()).return_code == 3
