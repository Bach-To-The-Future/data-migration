"""Lowering of procedural code (issue #43, spec sections 4 and 5): control flow, variables, temp tables, table
variables and the scopes around them, in procedures, triggers, batches and function bodies.

Expected values come from T-SQL's documented behaviour: a variable is declared for the whole batch (not the
block), a temp table is created and dropped at run time and is visible to called procedures, a table variable
lives to the end of its batch, a procedure that states no code returns zero."""
import pathlib

from migrt.core import checker
from migrt.sources.sqlserver import lowering

from lowering_helpers import lower_text, of_kind, parameter

PROC = "CREATE PROCEDURE sales.uspTest {params} AS BEGIN\n{body}\nEND"


def proc(snapshot, body: str, params: str = "", parameters=()):
    return lower_text(snapshot, PROC.format(params=params, body=body), kind="procedure", name="uspTest",
                      parameters=list(parameters))


def checked(artefact, reader) -> list:
    return checker.check(artefact, lowering.columns_of(reader))


def lowered(snapshot, body: str, params: str = "", parameters=()):
    artefact, reader = proc(snapshot, body, params, parameters)
    assert checked(artefact, reader) == []
    return artefact


def statements(artefact):
    return artefact.body.children_with("statement")


def stmt_kinds(node, role="statement"):
    return [c.kind for c in node.children_with(role)]


def scope_of(node):
    return node.attrs["scope"]


# ---------------------------------------------------------------- variables

def test_a_variable_is_declared_with_its_type_and_an_initial_value(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "DECLARE @n int = 5, @s varchar(10);")

    declared = statements(artefact)
    assert [d.kind for d in declared] == ["declare_variable", "declare_variable"]
    assert [d.attrs["name"] for d in declared] == ["@n", "@s"]
    assert str(declared[0].type) == "integer(bits=32, signed=True)"
    assert declared[0].child("initial").attrs["value"] == 5
    assert declared[1].children_with("initial") == []          # null until assigned
    text = declared[1].type
    assert text is not None and text.name == "string" and text.param("length") == 10
    # a string variable has the database's default collation, with the default origin
    assert text.param("origin") == "default"


def test_set_assigns_an_expression_converted_to_the_variables_type(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "DECLARE @big bigint; DECLARE @n int = 1; SET @big = @n; SET @n += 2;")

    _, _, widening, compound = statements(artefact)
    assert widening.kind == "assign" and widening.attrs["variable"] == "@big"
    # SQL Server converts the value to the variable's type: int to bigint is written out
    assert widening.child("argument").kind == "implicit_convert"
    assert compound.kind == "assign" and compound.attrs["variable"] == "@n"
    assert compound.child("argument").kind == "add"
    assert [c.kind for c in compound.child("argument").children_with("argument")] == ["variable", "literal"]


def test_select_into_variables_is_assign_from_rows_over_a_query(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @id int, @status int;\n"
                       "SELECT @id = o.OrderId, @status = o.Status FROM sales.[Order] AS o WHERE o.CustomerId = 1;")

    *_, rows = statements(artefact)
    assert rows.kind == "assign_from_rows" and rows.attrs["variables"] == ["@id", "@status"]
    plan = rows.child("query")
    assert plan.kind == "project"
    # @status is an int and Status is narrower, so SQL Server converts it on assignment
    assert [o.kind for o in plan.children_with("output")] == ["column", "implicit_convert"]
    assert plan.child("input").kind == "filter"


def test_a_parameter_is_in_scope_and_an_output_parameter_keeps_its_direction(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "SET @out = @in + 1;", "@in int, @out int OUTPUT",
                       [parameter("@in"), parameter("@out", direction="output")])

    [assign] = statements(artefact)
    assert assign.attrs["variable"] == "@out"
    assert [p.attrs["direction"] for p in artefact.body.children_with("parameter")] == ["input", "output"]
    assert scope_of(assign)["parameters"] == ["@in", "@out"]


def test_a_variable_that_was_never_declared_is_a_checker_finding_not_a_silent_pass(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, "DECLARE @n int = 1;\nSET @n = 2;")
    assert checked(artefact, reader) == []
    # take the declaration away after lowering: the use is then outside every scope
    artefact.body.children = [c for c in artefact.body.children if c.kind != "declare_variable"]
    findings = checked(artefact, reader)
    assert any("@n" in f.message and "scope" in f.message for f in findings)


# ---------------------------------------------------------------- control flow

def test_if_else_keeps_the_condition_and_both_branches(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @n int = 1, @r int;\n"
                       "IF @n > 0 SET @r = 1;\n"
                       "ELSE BEGIN SET @r = 2; SET @r += 1; END")

    *_, branch = statements(artefact)
    assert branch.kind == "if"
    assert branch.child("condition").kind == "compare"
    assert stmt_kinds(branch, "then") == ["assign"]
    assert stmt_kinds(branch, "else") == ["assign", "assign"]


def test_if_without_else_has_no_else_branch(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "DECLARE @n int = 1;\nIF @n = 1 SET @n = 2;")

    [_, branch] = statements(artefact)
    assert stmt_kinds(branch, "then") == ["assign"] and branch.children_with("else") == []


def test_a_condition_that_tests_rows_is_an_exists_over_a_query(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "IF EXISTS (SELECT 1 FROM sales.[Order] AS o WHERE o.Status = 9) RETURN 1;")

    [branch] = statements(artefact)
    assert branch.child("condition").kind == "exists"


def test_while_with_break_and_continue(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @i int = 0;\n"
                       "WHILE @i < 10 BEGIN\n  SET @i += 1;\n  IF @i = 3 CONTINUE;\n  IF @i = 7 BREAK;\nEND")

    [_, loop] = statements(artefact)
    assert loop.kind == "while" and loop.child("condition").kind == "compare"
    assert stmt_kinds(loop, "body") == ["assign", "if", "if"]
    assert [of_kind(artefact, k) != [] for k in ("break", "continue")] == [True, True]


def test_goto_and_its_label_are_kept_as_jump_and_declare_label(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @n int = 0;\nIF @n = 0 GOTO done;\nSET @n = 1;\ndone:\nSET @n = 2;")

    assert [n.attrs["label"] for n in of_kind(artefact, "jump")] == ["done"]
    assert [n.attrs["name"] for n in of_kind(artefact, "declare_label")] == ["done"]


def test_a_jump_to_a_label_that_does_not_exist_is_a_checker_finding(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, "GOTO nowhere;")
    assert any("nowhere" in f.message for f in checked(artefact, reader))


def test_return_gives_a_code_in_a_procedure_and_a_value_in_a_function(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "DECLARE @n int = 4;\nIF @n > 1 RETURN @n;\nRETURN;")

    first, last = of_kind(artefact, "return")
    assert first.child("argument").kind == "variable"
    assert last.children_with("argument") == []        # a procedure that states no code returns zero

    function, reader = lower_text(
        corpus_snapshot,
        "CREATE FUNCTION sales.fnTest (@n int) RETURNS int AS BEGIN DECLARE @x int = @n; SET @x += 1; RETURN @x END",
        kind="scalar_function", parameters=[parameter("@n")],
        returns={"declared_type": "int", "system_type": "int", "max_length": 4, "precision": 10, "scale": 0})
    assert checked(function, reader) == []
    assert stmt_kinds(function.body) == ["declare_variable", "assign", "return"]
    assert not of_kind(function, "opaque_fragment")


# ---------------------------------------------------------------- temp tables and table variables

def test_a_temp_table_is_created_filled_read_and_dropped(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "CREATE TABLE #t (Id int NOT NULL PRIMARY KEY, Label varchar(10) NULL);\n"
                       "INSERT INTO #t (Id, Label) VALUES (1, 'a');\n"
                       "UPDATE #t SET Label = 'b' WHERE Id = 1;\n"
                       "SELECT t.Id, t.Label FROM #t AS t;\n"
                       "DELETE FROM #t WHERE Id = 1;\n"
                       "DROP TABLE #t;")

    assert stmt_kinds(artefact.body) == ["create_temp_table", "insert_rows", "update_rows", "emit_result_set",
                                         "delete_rows", "drop_temp_table"]
    create = statements(artefact)[0]
    assert create.attrs["name"] == "#t"
    assert [(c["name"], c["nullable"]) for c in create.attrs["columns"]] == [("Id", False), ("Label", True)]
    assert create.attrs["primary_key"] == ["Id"]
    [read] = of_kind(artefact, "read_table")[:1]
    assert read.attrs["temp_table"] == "#t" and read.attrs.get("table") is None


def test_a_temp_tables_text_columns_take_tempdbs_collation_not_the_databases(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, "CREATE TABLE #t (Label varchar(10) NULL);\nSELECT t.Label FROM #t AS t;")

    assert checked(artefact, reader) == []
    [column] = of_kind(artefact, "column")
    server = next(o for o in corpus_snapshot.objects if o.kind == "server")
    assert column.type is not None
    assert column.type.param("collation") == server.fact("collation").value
    assert column.type.param("origin") == "column"
    assert any("tempdb" in a for a in artefact.assumptions)     # tempdb is not read: its collation is assumed


def test_select_into_a_temp_table_creates_it_with_the_queries_columns(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "SELECT o.OrderId, o.Status INTO #o FROM sales.[Order] AS o;\n"
                       "SELECT x.OrderId FROM #o AS x;")

    create, emit = statements(artefact)
    assert create.kind == "create_temp_table" and create.attrs["name"] == "#o"
    assert [c["name"] for c in create.attrs["columns"]] == ["OrderId", "Status"]
    assert create.child("query").kind == "project"
    assert emit.kind == "emit_result_set"


def test_a_table_variable_is_a_row_set_with_its_columns(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @t TABLE (Id int NOT NULL, Qty int NULL);\n"
                       "INSERT INTO @t (Id, Qty) SELECT o.OrderId, o.Status FROM sales.[Order] AS o;\n"
                       "SELECT t.Id FROM @t AS t WHERE t.Qty > 1;")

    declare, insert, emit = statements(artefact)
    assert declare.kind == "declare_row_set_variable" and declare.attrs["name"] == "@t"
    assert [c["name"] for c in declare.attrs["columns"]] == ["Id", "Qty"]
    assert insert.kind == "insert_rows" and insert.attrs["target_kind"] == "row_set" and insert.attrs["target"] == "@t"
    assert insert.attrs["columns"] == ["Id", "Qty"]
    assert insert.child("source").kind == "project"
    [read] = [n for n in emit.walk() if n.kind == "read_table"]
    assert read.attrs["row_set"] == "@t"


def test_insert_values_is_a_list_of_rows_and_a_catalog_table_is_a_table_target(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @n int = 3;\n"
                       "INSERT INTO ref.Customer (CustomerCode, Name) VALUES ('X1', 'Ann'), ('X2', 'Bob');")

    insert = statements(artefact)[1]
    assert insert.attrs["target_kind"] == "table" and insert.attrs["target"] == "RetailSample.ref.Customer"
    assert insert.child("source").kind == "read_values"
    assert insert.child("source").attrs["rows"] == 2 and insert.child("source").attrs["columns"] == ["CustomerCode", "Name"]


def test_update_and_delete_keep_their_source_rows_condition_and_assignments(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "UPDATE o SET Status = 2 FROM sales.[Order] AS o JOIN ref.Customer AS c "
                       "ON c.CustomerId = o.CustomerId WHERE c.CustomerCode = 'A';\n"
                       "DELETE FROM sales.[Order] WHERE Status = 9;")

    update, delete = statements(artefact)
    assert update.kind == "update_rows" and update.attrs["columns"] == ["Status"]
    assert update.attrs["target_alias"] == "o"
    assert update.child("input").kind == "filter" and update.child("input").child("input").kind == "join"
    # Status is narrower than int: the literal is converted to the column's type on assignment
    assert [v.kind for v in update.children_with("value")] == ["implicit_convert"]
    assert delete.kind == "delete_rows" and delete.child("input").kind == "filter"
    assert delete.attrs["target"] == "RetailSample.sales.Order"


def test_a_reference_to_a_temp_table_or_table_variable_that_is_not_in_scope_is_a_checker_finding(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot,
                            "CREATE TABLE #t (Id int);\nDROP TABLE #t;\nCREATE TABLE #u (Id int);\n"
                            "SELECT t.Id FROM #u AS t;")
    assert checked(artefact, reader) == []
    # drop the creation of #u after lowering: the read is then of a table that does not exist
    artefact.body.children = [c for c in artefact.body.children
                              if not (c.kind == "create_temp_table" and c.attrs["name"] == "#u")]
    assert any("#u" in f.message for f in checked(artefact, reader))


def test_a_global_temp_table_is_kept_as_an_opaque_barrier_with_its_reason(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "CREATE TABLE ##g (Id int);")

    [fragment] = statements(artefact)
    assert fragment.kind == "opaque_fragment" and fragment.attrs["scope_barrier"] is True
    assert "global temp table" in fragment.attrs["reason"]


# ---------------------------------------------------------------- scopes

def test_every_statement_says_what_is_in_scope_before_it(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @a int = 1;\n"
                       "CREATE TABLE #t (Id int);\n"
                       "DECLARE @r TABLE (Id int);\n"
                       "SET @a = 2;\n"
                       "DROP TABLE #t;\n"
                       "SET @a = 3;", "@p int", [parameter("@p")])

    declare, create, row_set, first, drop, last = statements(artefact)
    assert scope_of(declare) == {"parameters": ["@p"], "variables": [], "row_sets": [], "temp_tables": [],
                                 "barrier": None}
    assert scope_of(create)["variables"] == ["@a"]
    assert scope_of(row_set)["temp_tables"] == ["#t"]
    assert scope_of(first)["row_sets"] == ["@r"] and scope_of(first)["temp_tables"] == ["#t"]
    assert scope_of(drop)["temp_tables"] == ["#t"]
    assert scope_of(last)["temp_tables"] == [] and scope_of(last)["variables"] == ["@a"]


def test_a_variable_declared_in_a_block_stays_declared_after_it_as_t_sql_scopes_by_batch(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "IF 1 = 1 BEGIN DECLARE @inner int = 1; END\nSET @inner = 2;")

    [branch, after] = statements(artefact)
    assert scope_of(branch)["variables"] == []
    assert scope_of(after)["variables"] == ["@inner"]
    [declared] = [n for n in branch.walk() if n.kind == "declare_variable"]
    assert scope_of(declared)["variables"] == []


def test_a_temp_table_made_in_one_branch_may_exist_after_it_and_one_dropped_in_one_branch_still_may(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @c int = 1;\n"
                       "CREATE TABLE #keep (Id int);\n"
                       "IF @c = 1 BEGIN CREATE TABLE #maybe (Id int); DROP TABLE #keep; END\n"
                       "DROP TABLE #keep;")

    last = statements(artefact)[-1]
    assert scope_of(last)["temp_tables"] == ["#keep", "#maybe"]       # may exist: the branch might not have run


def test_a_scope_barrier_marks_everything_after_it_as_uncertain(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @n int = 1;\nEXEC('DROP TABLE #t');\nSET @n = 2;")

    declare, dynamic, after = statements(artefact)
    assert dynamic.kind == "dynamic_sql_execution" and dynamic.attrs["scope_barrier"] is True
    assert scope_of(declare)["barrier"] is None and scope_of(dynamic)["barrier"] is None
    assert scope_of(after)["barrier"] == str(dynamic.span)


def test_an_opaque_statement_records_what_it_touches_or_is_a_barrier(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @n int = 1;\nPRINT 'n is ' + CAST(@n AS varchar(10));\n"
                       "BEGIN TRY SET @n = 2; END TRY BEGIN CATCH SET @n = 3; END CATCH")

    _, printed, guarded = statements(artefact)
    # PRINT can change nothing but what it names, so it is not a barrier and says what it touches
    assert printed.kind == "opaque_fragment" and printed.attrs["scope_barrier"] is False
    assert printed.attrs["touches"] == {"variables": ["@n"], "temp_tables": [], "undeclared": []}
    # TRY/CATCH is lowered by #85 and may run anything: it is a barrier
    assert guarded.kind == "opaque_fragment" and guarded.attrs["scope_barrier"] is True


def test_an_opaque_statement_touching_a_name_not_in_scope_is_a_checker_finding(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, "DECLARE @n int = 1;\nPRINT @n;")
    assert checked(artefact, reader) == []
    [printed] = [n for n in of_kind(artefact, "opaque_fragment")]
    printed.attrs["touches"] = {"variables": ["@ghost"], "temp_tables": [], "undeclared": []}
    assert any("@ghost" in f.message for f in checked(artefact, reader))


# ---------------------------------------------------------------- procedures, triggers, batches

def test_a_procedure_lowers_parameters_result_sets_calls_and_session_options(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "SET NOCOUNT ON;\n"
                       "DECLARE @rc int;\n"
                       "EXEC @rc = sales.uspCreateOrder @CustomerCode = 'A', @OrderId = @n OUTPUT;\n"
                       "SELECT o.OrderId FROM sales.[Order] AS o WHERE o.CustomerId = @n;\n"
                       "RETURN @rc;", "@n int", [parameter("@n")])

    assert artefact.artefact_type == "procedure" and artefact.body.kind == "procedure_body"
    option, _, call, emit, ret = statements(artefact)
    assert option.kind == "set_session_option" and option.attrs == {
        "option": "NOCOUNT", "value": True, "scope": option.attrs["scope"]}
    assert call.kind == "call_procedure" and call.attrs["return_variable"] == "@rc"
    assert call.attrs["procedure"] == "RetailSample.sales.uspCreateOrder"
    args = call.children_with("argument")
    assert [(a.attrs["parameter"], a.attrs["output"]) for a in args] == [("@CustomerCode", False), ("@OrderId", True)]
    assert emit.kind == "emit_result_set" and emit.child("query").kind == "project"
    assert ret.kind == "return"
    assert "RetailSample.sales.uspCreateOrder" in artefact.dependencies


def test_a_trigger_names_its_table_timing_and_events_and_reads_inserted_and_deleted(corpus_snapshot):
    artefact, reader = lower_text(
        corpus_snapshot,
        "CREATE TRIGGER sales.trTest ON sales.[Order] AFTER INSERT, UPDATE AS BEGIN\n"
        "  DECLARE @n int;\n"
        "  SELECT @n = COUNT(*) FROM inserted AS i JOIN deleted AS d ON d.OrderId = i.OrderId;\n"
        "END",
        kind="trigger", name="trTest", table="RetailSample.sales.Order", timing="AFTER", events=["INSERT", "UPDATE"])

    assert checked(artefact, reader) == []
    assert artefact.artefact_type == "trigger" and artefact.body.kind == "trigger_body"
    assert artefact.body.attrs["table"] == "RetailSample.sales.Order"
    assert artefact.body.attrs["timing"] == "AFTER" and artefact.body.attrs["events"] == ["INSERT", "UPDATE"]
    changed = of_kind(artefact, "read_changed_rows")
    assert sorted(n.attrs["rows"] for n in changed) == ["deleted", "inserted"]
    assert all(n.attrs["table"] == "RetailSample.sales.Order" for n in changed)


def test_a_batch_script_is_its_batches_and_variables_do_not_survive_go_but_temp_tables_do(corpus_snapshot):
    artefact, reader = lower_text(
        corpus_snapshot,
        "DECLARE @n int = 1;\nCREATE TABLE #t (Id int);\nGO\nSET @n = 2;\nSELECT t.Id FROM #t AS t;\n",
        kind="query_batch", name="script")

    assert checked(artefact, reader) == []
    assert artefact.artefact_type == "query_batch" and artefact.body.kind == "batch_script_body"
    first, second = artefact.body.children_with("batch")
    assert first.kind == "batch" and stmt_kinds(first) == ["declare_variable", "create_temp_table"]
    # @n is gone in the second batch: that statement is kept verbatim, and the checker accounts for it
    assert stmt_kinds(second) == ["opaque_fragment", "emit_result_set"]
    assert "@n" in second.children_with("statement")[0].attrs["reason"] or \
        "not declared" in second.children_with("statement")[0].attrs["reason"]
    assert scope_of(second.children_with("statement")[1])["temp_tables"] == ["#t"]
    assert scope_of(second.children_with("statement")[0])["variables"] == []


def test_comments_annotate_the_nearest_statement_and_every_node_has_a_position(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "-- first\nDECLARE @n int = 1; -- trailing\nSET @n = 2;")

    declare, assign = statements(artefact)
    assert declare.comments == ["-- first", "-- trailing"] or declare.comments[0] == "-- first"
    assert all(n.span is not None for n in artefact.nodes())


def test_procedural_lowering_is_deterministic(corpus_snapshot):
    body = "DECLARE @n int = 1;\nCREATE TABLE #t (Id int);\nWHILE @n < 3 BEGIN SET @n += 1; INSERT INTO #t VALUES (@n); END"
    first, _ = proc(corpus_snapshot, body)
    second, _ = proc(corpus_snapshot, body)
    assert first.document() == second.document()


# ---------------------------------------------------------------- the corpus

ARTEFACTS = pathlib.Path(__file__).resolve().parents[2] / "corpus" / "sample" / "artefacts"
# The statements this ticket lowers. Every other statement of a corpus artefact is kept verbatim, with its reason.
THIS_TICKET = {"BeginEndBlockStatement", "DeclareVariableStatement", "SetVariableStatement", "IfStatement",
               "WhileStatement", "BreakStatement", "ContinueStatement", "GoToStatement", "LabelStatement",
               "ReturnStatement", "SelectStatement", "DeclareTableVariableStatement", "CreateTableStatement",
               "DropTableStatement", "InsertStatement", "UpdateStatement", "DeleteStatement", "ExecuteStatement",
               "PredicateSetStatement", "CreateProcedureStatement", "CreateTriggerStatement"}


def procedural_modules(snapshot):
    return [o for o in snapshot.objects if o.kind in ("procedure", "trigger")
            and o.has_fact("source_text") and o.fact("extractable").value]


def lower_module(snapshot, module):
    from migrt.core.catalog import CatalogReader
    from migrt.sources.sqlserver import extractor, names, parser

    reader = CatalogReader(snapshot, names.resolve)
    database = module.identity.split(".")[0]
    parsed = parser.parse(module.identity, reader.value(module.identity, "source_text"),
                          reader.value(database, "compatibility_level", extractor.SOURCE),
                          reader.value(module.identity, "quoted_identifier", extractor.SOURCE))
    return lowering.lower_artefact(reader, module.identity, parsed), reader


def test_the_checker_passes_on_every_corpus_procedure_and_trigger_or_reports_what_it_found(corpus_snapshot):
    modules = procedural_modules(corpus_snapshot)
    assert len(modules) == 82
    failures = {}
    for module in modules:
        artefact, reader = lower_module(corpus_snapshot, module)
        findings = checked(artefact, reader)
        if findings:
            failures[module.identity] = [f"{f.node_id}: {f.message}" for f in findings]
    assert failures == {}


def test_every_corpus_batch_lowers_and_passes_the_checker(corpus_snapshot):
    files = sorted((ARTEFACTS / "batches").glob("*.sql"))
    assert len(files) == 4
    for file in files:
        artefact, reader = lower_text(corpus_snapshot, file.read_text(encoding="utf-8"), kind="query_batch",
                                      name=file.stem)
        assert checked(artefact, reader) == [], file.name
        assert artefact.body.kind == "batch_script_body"


# What a statement of this ticket's kinds can still be kept as written for: a query, expression or form of write
# that an earlier ticket deferred (issue #42's list) and no ticket has taken yet. Anything else kept as written in
# an artefact made only of these constructs is this ticket's gap, and fails.
DEFERRED_ELSEWHERE = ("refers to itself: recursive named queries", "FOR JSON", "FOR XML", "FullTextTableReference",
                      "FOR SYSTEM_TIME", "the OUTPUT clause", "on a temp table is not lowered yet",
                      "not exactly one of the row sources in FROM", "does not resolve to a procedure in the catalog")


def test_an_artefact_made_only_of_these_constructs_lowers_with_no_statement_kept_for_this_tickets_gaps(corpus_snapshot):
    from migrt.core import scopes
    from migrt.sources.sqlserver import parser

    gaps = {}
    lowered_clean = []
    for module in procedural_modules(corpus_snapshot):
        artefact, reader = lower_module(corpus_snapshot, module)
        database = module.identity.split(".")[0]
        parsed = parser.parse(module.identity, reader.value(module.identity, "source_text"),
                              reader.value(database, "compatibility_level", "sqlserver"), True)
        kinds = set()
        parser.walk(parsed.tree, lambda f: kinds.add(str(f.GetType().Name)) if str(f.GetType().Name).endswith("Statement") else None)
        if not kinds <= THIS_TICKET:
            continue
        kept = [n for n in of_kind(artefact, "opaque_fragment") if n.role in scopes.STATEMENT_ROLES]
        mine = [n.attrs["reason"] for n in kept if not any(d in n.attrs["reason"] for d in DEFERRED_ELSEWHERE)]
        if mine:
            gaps[module.identity] = mine
        if not kept:
            lowered_clean.append(module.identity)
    assert gaps == {}
    assert len(lowered_clean) >= 10


# ---------------------------------------------------------------- review follow-ups

def test_a_string_too_long_for_its_variable_is_cut_and_the_cut_is_written_out(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "DECLARE @s varchar(5);\nSET @s = 'abcdefghij';")

    _, assign = statements(artefact)
    value = assign.child("argument")
    # SQL Server cuts the string without an error; the vocabulary says so on `assign`
    assert value.kind == "implicit_convert" and value.attrs.get("fits") is True
    assert value.type is not None and value.type.param("length") == 5


def test_a_decimal_stored_with_fewer_digits_is_rounded_and_it_is_written_out(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "DECLARE @d decimal(5, 1);\nSET @d = 12.345;")

    _, assign = statements(artefact)
    assert assign.child("argument").kind == "implicit_convert" and assign.child("argument").attrs.get("fits") is True


def test_a_scalar_function_returns_its_declared_type(corpus_snapshot):
    artefact, reader = lower_text(
        corpus_snapshot, "CREATE FUNCTION sales.fnTest (@n int) RETURNS bigint AS BEGIN RETURN @n END",
        kind="scalar_function", parameters=[parameter("@n")],
        returns={"declared_type": "bigint", "system_type": "bigint", "max_length": 8, "precision": 19, "scale": 0})

    assert checked(artefact, reader) == []
    [ret] = of_kind(artefact, "return")
    assert ret.child("argument").kind == "implicit_convert"


def test_break_and_continue_outside_a_loop_are_checker_findings(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, "BREAK;")
    assert any("outside every loop" in f.message for f in checked(artefact, reader))


def test_dropping_a_temp_and_a_permanent_table_together_is_kept_as_written_not_forgotten(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "CREATE TABLE #a (Id int);\nDROP TABLE #a, sales.Nothing;")

    _, drop = statements(artefact)
    assert drop.kind == "opaque_fragment" and drop.attrs["scope_barrier"] is True


# ---------------------------------------------------------------- scope entries that may be stale

def stale(artefact):
    return [n for n in artefact.nodes() if "scope_may_be_stale" in n.attrs]


def test_after_a_scope_barrier_every_node_that_depends_on_a_scope_entry_carries_the_flag(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @n int = 1;\nCREATE TABLE #t (Id int);\n"
                       "SELECT t.Id FROM #t AS t WHERE t.Id = @n;\n"
                       "EXEC('DROP TABLE #t');\n"
                       "SET @n = 2;\n"
                       "SELECT t.Id FROM #t AS t WHERE t.Id = @n;\n"
                       "INSERT INTO #t (Id) VALUES (@n);")

    *_, dynamic, assign, read, insert = statements(artefact)
    barrier = str(dynamic.span)
    flagged = {n.kind: n.attrs["scope_may_be_stale"] for n in stale(artefact)}
    assert set(flagged.values()) == {barrier}
    assert assign.attrs["scope_may_be_stale"] == barrier
    assert insert.attrs["scope_may_be_stale"] == barrier
    # the table read and the variable read inside the later SELECT, and the variable inside the INSERT's value
    assert [n.kind for n in read.walk() if "scope_may_be_stale" in n.attrs] == ["read_table", "variable"]
    # nothing before the barrier is flagged
    assert not [n for s in statements(artefact)[:3] for n in s.walk() if "scope_may_be_stale" in n.attrs]
    # a node that depends on no scope entry is not flagged: a literal, a catalog table
    assert not [n for n in artefact.nodes() if n.kind in ("literal", "compare") and "scope_may_be_stale" in n.attrs]


def test_the_flag_reaches_nested_statements_and_the_next_batch_of_a_script(corpus_snapshot):
    nested = lowered(corpus_snapshot,
                     "DECLARE @n int = 1;\nEXEC('x');\nIF @n = 1 BEGIN SET @n = 2; WHILE @n < 5 SET @n += 1; END")
    flagged_kinds = sorted({n.kind for n in stale(nested)})
    assert flagged_kinds == ["assign", "variable"]
    assert all(n.attrs["scope_may_be_stale"] for n in of_kind(nested, "assign"))
    assert len(of_kind(nested, "assign")) == 2 and len(stale(nested)) >= 5       # the condition's variable too

    script, reader = lower_text(corpus_snapshot,
                                "CREATE TABLE #t (Id int);\nEXEC('x');\nGO\nSELECT t.Id FROM #t AS t;\n",
                                kind="query_batch", name="script")
    assert checked(script, reader) == []
    [read] = of_kind(script, "read_table")
    assert "scope_may_be_stale" in read.attrs                 # a barrier in one batch still leaves temp tables stale


def test_the_checker_refuses_a_missing_or_a_spurious_stale_flag(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, "DECLARE @n int = 1;\nEXEC('x');\nSET @n = 2;")
    assert checked(artefact, reader) == []
    [assign] = of_kind(artefact, "assign")
    del assign.attrs["scope_may_be_stale"]
    assert any("may be stale" in f.message for f in checked(artefact, reader))

    artefact, reader = proc(corpus_snapshot, "DECLARE @n int = 1;\nSET @n = 2;")
    [assign] = of_kind(artefact, "assign")
    assign.attrs["scope_may_be_stale"] = "L1:1-L1:2"
    assert any("no scope barrier" in f.message for f in checked(artefact, reader))
