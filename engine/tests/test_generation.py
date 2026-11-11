"""Generation for the PySpark target (issue #44, spec sections 5 and 6): views, inline functions and
column-expression functions generate; everything else is reported as manual work, never dropped; constructs the
target cannot translate are detected before generation; each linked expression is one self-contained span.

Expected values are the spec's: a view or a table-valued function returns a DataFrame, a scalar function
without data access builds a column expression, a procedure or trigger or batch is procedural generation
(issue #50) and is reported here, and what T-SQL computes (integer division truncates toward zero, a CASE
without ELSE gives NULL) is what the column expression must compute on a real Spark."""
import ast
import re

import pytest

from generation_helpers import generate_text
from lowering_helpers import lower_text, parameter
from migrt.core import vocabulary
from migrt.targets.pyspark import generator, untranslatable

VIEW = "CREATE VIEW sales.vTest AS SELECT o.OrderId, o.Status + 1 AS Next FROM sales.[Order] AS o WHERE o.Status <> 9"
RATIO = ("CREATE FUNCTION ref.fnSafeRatio (@Numerator int, @Denominator int) RETURNS int AS BEGIN\n"
         "RETURN CASE WHEN @Denominator = 0 THEN NULL ELSE @Numerator / @Denominator END; END")
ALL_KINDS_SCRIPT = (
    "DECLARE @n int = 1;\nDECLARE @s varchar(5);\nDECLARE @rc int;\nDECLARE @t TABLE (Id int NOT NULL, Qty int NULL);\n"
    "CREATE TABLE #w (Id int NOT NULL);\nSET NOCOUNT ON;\nSET XACT_ABORT ON;\nSET @s = 'abcdefghij';\n"
    "SELECT @n = COUNT(*) FROM sales.OrderLine AS ol;\nINSERT INTO #w (Id) VALUES (@n);\n"
    "INSERT INTO @t (Id, Qty) SELECT ol.OrderId, ol.Quantity FROM sales.OrderLine AS ol;\n"
    "UPDATE #w SET Id = 2 WHERE Id = 1;\nDELETE FROM #w WHERE Id = 2;\n"
    "EXEC @rc = sales.uspCreateOrder @CustomerCode = 'A', @OrderId = @n OUTPUT;\n"
    "SELECT w.Id FROM #w AS w;\nIF @n = 1 GOTO done;\n"
    "WHILE @n < 5 BEGIN SET @n += 1; IF @n = 3 CONTINUE; IF @n = 4 BREAK; END;\ndone:\nDROP TABLE #w;\nGO\n"
    "SELECT 1 AS One;")
# The node kinds issue #43 added, which the ticket says generation handles or reports as manual work, never drops.
NEW_KINDS = {"batch", "declare_variable", "declare_row_set_variable", "assign", "assign_from_rows", "if", "while",
             "break", "continue", "jump", "declare_label", "create_temp_table", "drop_temp_table", "emit_result_set",
             "insert_rows", "update_rows", "delete_rows", "call_procedure", "set_session_option",
             "set_abort_on_error", "read_changed_rows"}


def manual_text(generated) -> str:
    return " ".join(m.reason for m in generated.file.manual_items)


def covered(generated) -> set[str]:
    """Ids of every node under a manual item's replaced subtree."""
    nodes = {n.id: n for n in generated.artefact.nodes()}
    return {n.id for m in generated.file.manual_items for n in nodes[m.replaces].walk()}


# ---------------------------------------------------------------- views and expressions

def test_every_linked_expression_of_a_view_is_one_self_contained_span(corpus_snapshot):
    generated = generate_text(corpus_snapshot, VIEW)

    assert generated.layer1.passed, generated.layer1.failures
    assert generated.file.manual_items == []
    lines = generated.text.splitlines()
    snippets = [lines[s.line - 1][s.start:s.end] for s in generated.file.expressions.values()]
    assert 'F.col("o.OrderId")' in snippets and len(snippets) >= 6
    for snippet in snippets:
        ast.parse(snippet, mode="eval")
    expected = {n.id for n in generated.artefact.nodes() if vocabulary.part_of(n.kind) == "expression"}
    assert expected <= set(generated.file.expressions)


def test_a_literal_inside_a_marked_secret_is_read_from_its_named_setting(corpus_snapshot):
    generated = generate_text(
        corpus_snapshot,
        "CREATE VIEW sales.vTest AS SELECT 'Server=hq;Password=Hunter2!;' AS Conn FROM sales.[Order] AS o")

    [setting] = generated.artefact.secret_settings
    assert generated.layer1.passed, generated.layer1.failures
    assert generated.file.manual_items == []
    assert f'ss.setting("{setting}")' in generated.text and "Hunter2" not in generated.text
    assert generated.file.settings_used == [setting]


def test_a_literal_holding_several_marked_secrets_is_manual_work_never_one_guessed_setting(corpus_snapshot):
    generated = generate_text(
        corpus_snapshot,
        "CREATE VIEW sales.vTest AS SELECT 'Server=hq;User ID=loader;Password=Hunter2!;' AS Conn FROM sales.[Order] AS o")

    assert len(generated.artefact.secret_settings) == 2
    assert "one setting cannot stand for all of them" in manual_text(generated)
    assert "Hunter2" not in generated.text and "ss.setting" not in generated.text
    assert generated.layer1.passed, generated.layer1.failures


def test_a_numeric_secret_is_read_as_text_and_cast_to_its_type(corpus_snapshot):
    from migrt.core.secrets import SecretSpan
    from migrt.sources.sqlserver import extractor
    import lowering_helpers

    sql = "CREATE VIEW sales.vTest AS SELECT 7 AS B FROM sales.[Order] AS o"
    original = lowering_helpers._secrets
    lowering_helpers._secrets = lambda text: extractor._marked("source_text", [SecretSpan(text.index("7"), 1, "test")])
    try:
        generated = generate_text(corpus_snapshot, sql)
    finally:
        lowering_helpers._secrets = original

    assert f'F.lit(ss.setting("{generated.artefact.secret_settings[0]}")).cast("int")' in generated.text


def test_a_view_reads_another_generated_view_by_import_and_a_function_by_import(corpus_snapshot):
    generated = generate_text(
        corpus_snapshot,
        "CREATE VIEW sales.vTest AS SELECT v.OrderId, ref.fnSafeRatio(v.Units, v.LineCount) AS Ratio "
        "FROM sales.vOrderTotals AS v",
        generated_in_project={"RetailSample.sales.vOrderTotals": "view", "RetailSample.ref.fnSafeRatio": "scalar_function"})

    assert generated.layer1.passed, generated.layer1.failures
    assert generated.file.manual_items == []
    assert "from converted.RetailSample.sales.views.vOrderTotals import vOrderTotals" in generated.text
    assert "from converted.RetailSample.ref.functions.fnSafeRatio import fnSafeRatio" in generated.text
    assert "vOrderTotals(ss).alias(" in generated.text and "fnSafeRatio(" in generated.text


def test_a_view_reading_a_view_that_is_not_in_the_project_is_manual_work_never_a_table_read(corpus_snapshot):
    generated = generate_text(corpus_snapshot,
                              "CREATE VIEW sales.vTest AS SELECT v.OrderId FROM sales.vOrderTotals AS v",
                              dependency_kinds={"RetailSample.sales.vOrderTotals": "view"})

    assert "spark.read.table" not in generated.text
    assert "vOrderTotals is a view that is not generated in this project" in manual_text(generated)
    assert generated.layer1.passed, generated.layer1.failures


# ---------------------------------------------------------------- functions

def test_an_inline_function_is_a_dataframe_function_of_its_parameters(corpus_snapshot):
    generated = generate_text(
        corpus_snapshot,
        "CREATE FUNCTION sales.fnTest (@OrderId int) RETURNS TABLE AS RETURN "
        "SELECT ol.OrderLineId, ol.Quantity FROM sales.OrderLine AS ol WHERE ol.OrderId = @OrderId",
        kind="inline_function", name="fnTest", parameters=[parameter("@OrderId")])

    assert generated.layer1.passed, generated.layer1.failures
    assert generated.file.manual_items == []
    assert "def fnTest(ss, OrderId) -> DataFrame:" in generated.text
    assert 'F.lit(OrderId).cast("int")' in generated.text


def test_a_scalar_function_without_data_access_is_a_column_expression(corpus_snapshot):
    generated = generate_text(corpus_snapshot, RATIO, kind="scalar_function", name="fnSafeRatio", schema="ref",
                              parameters=[parameter("@Numerator"), parameter("@Denominator")], returns=_int())

    assert generated.layer1.passed, generated.layer1.failures
    assert generated.file.manual_items == []
    assert "def fnSafeRatio(ss, Numerator, Denominator) -> Column:" in generated.text
    assert "spark.read" not in generated.text


def test_the_column_expression_computes_what_tsql_computes_on_a_real_spark(corpus_snapshot):
    """7 / 2 is 3 and -7 / 2 is -3 in T-SQL (integer division truncates toward zero); a zero divisor gives NULL,
    not an error; a NULL operand gives NULL."""
    pyspark_sql = pytest.importorskip("pyspark.sql")
    spark = pyspark_sql.SparkSession.builder.master("local[1]").config("spark.ui.enabled", "false").getOrCreate()
    generated = generate_text(corpus_snapshot, RATIO, kind="scalar_function", name="fnSafeRatio", schema="ref",
                              parameters=[parameter("@Numerator"), parameter("@Denominator")], returns=_int())
    namespace: dict = {}
    exec(compile(generated.text, "fnSafeRatio.py", "exec"), namespace)

    class Session:
        pass

    session = Session()
    session.spark = spark                      # type: ignore[attr-defined]
    F = pyspark_sql.functions
    rows = spark.createDataFrame([(7, 2), (-7, 2), (7, 0), (None, 2), (7, None)], "a int, b int")
    result = rows.select(namespace["fnSafeRatio"](session, F.col("a"), F.col("b")).alias("r"))

    assert [r["r"] for r in result.collect()] == [3, -3, None, None, None]
    assert result.schema["r"].dataType.simpleString() == "int"


def test_a_scalar_function_with_control_flow_reports_each_statement_and_fails_when_called(corpus_snapshot):
    generated = generate_text(
        corpus_snapshot,
        "CREATE FUNCTION ref.fnTest (@x int) RETURNS int AS BEGIN\nDECLARE @y int = @x;\n"
        "IF @y > 1 SET @y = 1;\nRETURN @y;\nEND",
        kind="scalar_function", name="fnTest", schema="ref", parameters=[parameter("@x")], returns=_int())

    assert generated.layer1.passed, generated.layer1.failures
    assert len(generated.file.manual_items) >= 3
    assert "def fnTest(ss, x) -> Column:" in generated.text and "raise RuntimeError" in generated.text
    assert {"declare_variable", "if", "return"} <= {n.kind for n in generated.artefact.nodes() if n.id in covered(generated)}


def test_a_multi_statement_table_function_is_manual_work_statement_by_statement(corpus_snapshot):
    generated = generate_text(
        corpus_snapshot,
        "CREATE FUNCTION ref.fnSplit (@List varchar(100)) RETURNS @Codes TABLE (Position int NOT NULL) AS BEGIN\n"
        "DECLARE @pos int = 0;\nWHILE @pos < 3 BEGIN SET @pos += 1; INSERT INTO @Codes (Position) VALUES (@pos); END;\n"
        "RETURN;\nEND", kind="table_function", name="fnSplit", schema="ref",
        parameters=[parameter("@List", "varchar", length=100)])

    assert generated.layer1.passed, generated.layer1.failures
    assert "def fnSplit(ss, List) -> DataFrame:" in generated.text
    assert "procedural generation" in manual_text(generated) and "#" not in manual_text(generated)
    assert {w for m in generated.file.manual_items for w in m.waits_for} == {"#50"}


# ---------------------------------------------------------------- procedures, triggers, batches

def test_a_procedure_keeps_its_parameters_and_reports_every_statement(corpus_snapshot):
    generated = generate_text(
        corpus_snapshot,
        "CREATE PROCEDURE sales.uspTest @n int, @name varchar(10) = 'x' AS BEGIN\nSET NOCOUNT ON;\n"
        "SELECT o.OrderId FROM sales.[Order] AS o WHERE o.CustomerId = @n;\nEND",
        kind="procedure", name="uspTest", parameters=[parameter("@n"), parameter("@name", "varchar", length=10)])

    assert generated.layer1.passed, generated.layer1.failures
    assert "def uspTest(ss, *, n, name) -> None:" in generated.text
    assert "set_session_option" in manual_text(generated) and "emit_result_set" in manual_text(generated)
    assert "procedural generation" in manual_text(generated) and "#" not in manual_text(generated)
    assert generated.file.manual_items and all(m.waits_for == ("#50",) for m in generated.file.manual_items)
    assert {n.id for n in generated.artefact.nodes() if n.kind != "declare_parameter" and vocabulary.part_of(n.kind)} \
        <= covered(generated)


def test_a_trigger_takes_the_inserted_and_deleted_rows(corpus_snapshot):
    generated = generate_text(
        corpus_snapshot,
        "CREATE TRIGGER sales.trTest ON sales.[Order] AFTER INSERT AS BEGIN\n"
        "DECLARE @n int;\nSELECT @n = COUNT(*) FROM inserted AS i;\nEND",
        kind="trigger", name="trTest", table="RetailSample.sales.Order", timing="AFTER", events=("INSERT",))

    assert generated.layer1.passed, generated.layer1.failures
    assert "def trTest(ss, inserted, deleted) -> None:" in generated.text
    assert "read_changed_rows" in manual_text(generated) or "assign_from_rows" in manual_text(generated)


def test_a_batch_script_is_one_function_per_go_batch_and_a_list_giving_their_order(corpus_snapshot):
    generated = generate_text(corpus_snapshot, "DECLARE @n int = 1;\nSELECT @n;\nGO\nSELECT 1 AS One;",
                              kind="query_batch", name="load")

    assert generated.layer1.passed, generated.layer1.failures
    assert generated.file.relative_path == "converted/RetailSample/batches/load.py"
    assert "def batch_1(ss) -> None:" in generated.text and "def batch_2(ss) -> None:" in generated.text
    assert "BATCHES = [batch_1, batch_2]" in generated.text


def test_every_node_kind_issue_43_added_is_generated_or_reported_as_manual_work(corpus_snapshot):
    generated = generate_text(corpus_snapshot, ALL_KINDS_SCRIPT, kind="query_batch", name="all_kinds")
    trigger = generate_text(corpus_snapshot, "CREATE TRIGGER sales.trTest ON sales.[Order] AFTER INSERT AS "
                            "SELECT i.OrderId FROM inserted AS i", kind="trigger", name="trTest",
                            table="RetailSample.sales.Order", timing="AFTER", events=("INSERT",))

    present = {n.kind for g in (generated, trigger) for n in g.artefact.nodes()}
    assert NEW_KINDS <= present, sorted(NEW_KINDS - present)
    for g in (generated, trigger):
        assert g.layer1.passed, g.layer1.failures
        dropped = [n.kind for n in g.artefact.nodes() if n.kind in NEW_KINDS and n.id not in covered(g)]
        assert dropped == []
    assert any(n.kind == "implicit_convert" and n.attrs.get("fits") for n in generated.artefact.nodes())


def test_a_node_after_a_scope_barrier_is_never_generated_as_if_its_scope_were_certain(corpus_snapshot):
    generated = generate_text(
        corpus_snapshot,
        "CREATE PROCEDURE sales.uspTest AS BEGIN\nDECLARE @n int = 1;\nEXEC('x');\nSET @n = 2;\nEND",
        kind="procedure", name="uspTest")

    flagged = [n for n in generated.artefact.nodes() if "scope_may_be_stale" in n.attrs]
    assert flagged
    for node in flagged:
        [item] = [m for m in generated.file.manual_items if m.node == node.id]
        assert f"may be stale after the scope barrier at {node.attrs['scope_may_be_stale']}" in item.reason
    assert generated.layer1.passed, generated.layer1.failures


# ---------------------------------------------------------------- detection before generation

def test_untranslatable_constructs_are_detected_before_generation_and_reported_with_their_reasons(corpus_snapshot):
    artefact, _ = lower_text(
        corpus_snapshot,
        "CREATE PROCEDURE sales.uspTest AS BEGIN\nDECLARE @n int = 1;\nEXEC('SELECT 1');\n"
        "GRANT SELECT ON sales.[Order] TO PUBLIC;\nCREATE INDEX IX ON sales.[Order] (OrderId);\n"
        "BEGIN TRY SET @n = 2; END TRY BEGIN CATCH SET @n = 3; END CATCH;\nEND", kind="procedure", name="uspTest")

    found = generator.detect(artefact)

    by_kind = {f.kind: f for f in found}
    assert {"dynamic_sql_execution", "permission_change", "schema_change", "opaque_fragment"} <= set(by_kind)
    assert "dynamic SQL" in by_kind["dynamic_sql_execution"].reason and "manual" in by_kind["dynamic_sql_execution"].reason
    assert all(f.reason and f.node for f in found)
    assert found == generator.detect(artefact)                                       # deterministic


def test_every_operation_the_generator_does_not_handle_is_detected(corpus_snapshot):
    handled = generator.handled_kinds()
    for kind in sorted(vocabulary.operations() | vocabulary.node_kinds()):
        assert kind in handled or untranslatable.reason_for_kind(kind, handled), kind


def test_a_construct_detected_but_found_nowhere_in_the_file_is_refused_not_dropped(corpus_snapshot):
    generated = generate_text(corpus_snapshot, VIEW)
    artefact = generated.artefact
    victim = next(n for n in artefact.nodes() if n.kind == "filter")
    finding = untranslatable.Finding(victim.id, "filter", "invented for this test")

    with pytest.raises(generator.DroppedConstruct, match=victim.id):
        generator.require_accounted([finding], generated.file.manual_items, artefact)


def test_a_cross_join_and_other_plan_steps_without_generation_are_manual_work_and_pass_layer_1(corpus_snapshot):
    generated = generate_text(corpus_snapshot,
                              "CREATE VIEW sales.vTest AS SELECT a.OrderId FROM sales.[Order] AS a CROSS JOIN sales.[Order] AS b")

    assert generated.layer1.passed, generated.layer1.failures
    assert "cross join" in manual_text(generated)
    assert re.search(r"Manual work 1:", generated.text)


def _int():
    return {"system_type": "int", "max_length": 4, "precision": 10, "scale": 0, "collation": None}


def test_a_parameter_whose_table_column_needs_a_companion_column_says_so_in_the_report(corpus_snapshot):
    """Spec section 6, policy 5: each datetimeoffset column has a companion column holding its offset, and 'every
    assumption and requirement is listed in the report'."""
    offset = {"name": "@When", "declared_type": "datetimeoffset", "system_type": "datetimeoffset", "max_length": 10,
              "precision": 34, "scale": 7, "direction": "input", "readonly": False, "nullable": True, "default": None}
    generated = generate_text(
        corpus_snapshot,
        "CREATE FUNCTION sales.fnTest (@When datetimeoffset(7)) RETURNS TABLE AS RETURN "
        "SELECT ol.OrderLineId FROM sales.OrderLine AS ol WHERE ol.OrderId = 1",
        kind="inline_function", name="fnTest", parameters=[offset])

    assert any("companion" in text and "@When" in text for text in generated.file.assumptions)


def test_a_conversion_that_cuts_or_rounds_is_detected_before_generation(corpus_snapshot):
    artefact, _ = lower_text(corpus_snapshot, "CREATE PROCEDURE sales.uspTest AS BEGIN\nDECLARE @s varchar(5);\n"
                             "SET @s = 'abcdefghij';\nEND", kind="procedure", name="uspTest")

    found = generator.detect(artefact)

    assert any(f.kind == "implicit_convert" and "cuts or rounds" in f.reason for f in found)
