"""Lowering of the write forms and procedural constructs 08a kept as written (issue #104, spec sections 4 and 5):
MERGE, the OUTPUT clause, TOP in a write, INSERT ... EXEC, global temp tables, and database-level (DDL) triggers.

Expected values come from T-SQL's documented behaviour: a MERGE clause is WHEN [NOT] MATCHED [BY TARGET | BY SOURCE]
(NOT MATCHED alone means BY TARGET), OUTPUT gives one row per row changed with `inserted` (after), `deleted` (before)
and, in a MERGE, `$action`; the rows a TOP of a write touches are not ordered (a write's TOP cannot have an ORDER BY);
a global temp table is visible to every session and lives until the session that made it ends."""
from __future__ import annotations

from migrt.core import scopes, vocabulary
from migrt.core.catalog import CatalogObject, Fact, Provenance, Snapshot
from migrt.sources.sqlserver import extractor

from procedural_helpers import checked, kinds, lowered, lowered_script, proc, scope_of
from procedural_helpers import statements as statements_of


def statements(artefact):
    """The statements of an artefact's body (or of a body node)."""
    return statements_of(getattr(artefact, "body", artefact))

CUSTOMER = "RetailSample.ref.Customer"
MERGE_CUSTOMERS = (
    "MERGE ref.Customer AS t\n"
    "USING (SELECT CustomerCode, Name, Region FROM stg.CustomerImport) AS s ON t.CustomerCode = s.CustomerCode\n"
    "WHEN MATCHED AND t.Name <> s.Name THEN UPDATE SET Name = s.Name\n"
    "WHEN NOT MATCHED BY TARGET THEN INSERT (CustomerCode, Name, Region) VALUES (s.CustomerCode, s.Name, s.Region)\n"
    "WHEN NOT MATCHED BY SOURCE AND t.Email IS NULL THEN DELETE")


def clause_summary(merge):
    return [(c.attrs["match"], c.attrs["action"]) for c in merge.children_with("clause")]


# ---------------------------------------------------------------- MERGE

def test_merge_lowers_each_when_clause_with_its_condition_and_action(corpus_snapshot):
    artefact = lowered(corpus_snapshot, MERGE_CUSTOMERS + ";")

    [merge] = statements(artefact)
    assert merge.kind == "merge_rows"
    assert (merge.attrs["target"], merge.attrs["target_kind"], merge.attrs["target_alias"]) == (CUSTOMER, "table", "t")
    assert merge.child("condition").kind == "compare"
    assert merge.child("source").kind == "named_query"      # a derived table
    assert clause_summary(merge) == [("matched", "update"), ("not_matched_by_target", "insert"),
                                     ("not_matched_by_source", "delete")]
    matched, inserted, deleted = merge.children_with("clause")
    assert all(c.kind == "merge_clause" for c in (matched, inserted, deleted))
    # the extra condition of a clause is kept, and a clause with none says so by having none
    assert matched.child("condition").kind == "compare"
    assert inserted.children_with("condition") == []
    assert deleted.child("condition").kind == "is_null"
    assert matched.attrs["columns"] == ["Name"] and len(matched.children_with("value")) == 1
    assert inserted.attrs["columns"] == ["CustomerCode", "Name", "Region"] and len(inserted.children_with("value")) == 3
    assert CUSTOMER in artefact.dependencies


def test_not_matched_without_by_is_by_target_and_by_source_is_never_lost(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "MERGE ref.Customer AS t USING stg.CustomerImport AS s ON t.CustomerCode = s.CustomerCode\n"
                       "WHEN NOT MATCHED THEN INSERT (CustomerCode, Name, Region) VALUES (s.CustomerCode, s.Name, s.Region)\n"
                       "WHEN NOT MATCHED BY SOURCE THEN DELETE;")

    [merge] = statements(artefact)
    assert clause_summary(merge) == [("not_matched_by_target", "insert"), ("not_matched_by_source", "delete")]
    assert merge.child("source").kind == "read_table"


def test_merge_under_a_common_table_expression_keeps_the_definition(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       ";WITH Src AS (SELECT CustomerCode, Name FROM stg.CustomerImport)\n"
                       "MERGE ref.Customer AS t USING Src AS s ON t.CustomerCode = s.CustomerCode\n"
                       "WHEN MATCHED THEN UPDATE SET Name = s.Name;")

    [merge] = statements(artefact)
    assert merge.kind == "merge_rows" and merge.child("source").attrs["named_query"] == "Src"
    assert [c.kind for c in merge.children_with("definition")] == ["named_query"]


def test_merge_into_a_temp_table_names_it_and_depends_on_scope(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "CREATE TABLE #t (Id int NOT NULL, Name varchar(20) NULL);\n"
                       "MERGE #t AS t USING (SELECT CustomerId AS Id, Name FROM ref.Customer) AS s ON t.Id = s.Id\n"
                       "WHEN MATCHED THEN UPDATE SET Name = s.Name\n"
                       "WHEN NOT MATCHED THEN INSERT (Id, Name) VALUES (s.Id, s.Name);")

    create, merge = statements(artefact)
    assert (merge.attrs["target"], merge.attrs["target_kind"]) == ("#t", "temp_table")
    assert scope_of(merge)["temp_tables"] == ["#t"] and scopes.depends_on_scope(merge)


def test_a_merge_that_cannot_be_lowered_is_kept_whole_and_names_every_clause(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "MERGE ref.Customer AS t USING stg.CustomerImport AS s ON t.CustomerCode = s.CustomerCode\n"
                       "WHEN MATCHED THEN UPDATE SET Name = s.Name\n"
                       "WHEN NOT MATCHED BY SOURCE THEN DELETE\n"
                       "WHEN NOT MATCHED BY TARGET THEN INSERT DEFAULT VALUES;")

    [kept] = statements(artefact)
    assert kept.kind == "opaque_fragment"
    assert "WHEN NOT MATCHED BY SOURCE" in kept.attrs["text"] and "DEFAULT VALUES" in kept.attrs["text"]


def test_a_merge_without_its_condition_or_clauses_is_a_checker_finding(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, MERGE_CUSTOMERS + ";")
    assert checked(artefact, reader) == []
    [merge] = statements(artefact.body)
    merge.children = [c for c in merge.children if c.role != "condition"]
    assert any("condition" in f.message for f in checked(artefact, reader))
    merge.children = [c for c in merge.children if c.role != "clause"]
    assert any("clause" in f.message for f in checked(artefact, reader))


def test_a_merge_clause_must_name_a_match_and_an_action_that_agree(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, MERGE_CUSTOMERS + ";")
    [merge] = statements(artefact.body)
    inserted = merge.children_with("clause")[1]
    inserted.attrs["match"] = "matched"          # SQL Server refuses an INSERT of a matched row
    assert any("insert" in f.message for f in checked(artefact, reader))


# ---------------------------------------------------------------- OUTPUT

def test_output_to_the_caller_gives_before_and_after_values(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "UPDATE o SET Status = 3 OUTPUT deleted.Status AS OldStatus, inserted.Status AS NewStatus, "
                       "inserted.OrderId FROM sales.[Order] AS o WHERE o.Status = 2;")

    [update] = statements(artefact)
    output = update.child("changed_rows")
    assert output.attrs["into_kind"] == "result_set" and output.attrs["into"] is None
    assert output.attrs["names"] == ["OldStatus", "NewStatus", "OrderId"]
    assert [c.attrs["column"] for c in output.children_with("value")] == ["Status", "Status", "OrderId"]
    assert [c.attrs["relation"] for c in output.children_with("value")] == ["deleted", "inserted", "inserted"]
    assert sorted(c.attrs["rows"] for c in output.children if c.kind == "read_changed_rows") == ["deleted", "inserted"]
    assert update.child("input").kind == "filter"          # the write itself is lowered as before


def test_an_insert_has_only_inserted_rows_and_a_delete_only_deleted(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "INSERT INTO ref.Customer (CustomerCode, Name, Region) OUTPUT inserted.CustomerId "
                       "VALUES ('A', 'a', 'EU');\n"
                       "DELETE FROM stg.CustomerImport OUTPUT deleted.CustomerCode WHERE CustomerCode IS NULL;")

    insert, delete = statements(artefact)
    assert [c.attrs["rows"] for c in insert.child("changed_rows").children if c.kind == "read_changed_rows"] == ["inserted"]
    assert [c.attrs["rows"] for c in delete.child("changed_rows").children if c.kind == "read_changed_rows"] == ["deleted"]


def test_output_into_a_table_variable_names_its_columns_and_is_a_scoped_write(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @Ids TABLE (OrderId int NOT NULL, OldStatus tinyint NULL);\n"
                       "UPDATE sales.[Order] SET Status = 3 OUTPUT inserted.OrderId, deleted.Status INTO @Ids "
                       "(OrderId, OldStatus) WHERE Status = 2;")

    _, update = statements(artefact)
    output = update.child("changed_rows")
    assert (output.attrs["into_kind"], output.attrs["into"], output.attrs["into_columns"]) == (
        "row_set", "@Ids", ["OrderId", "OldStatus"])
    assert scope_of(update)["row_sets"] == ["@Ids"] and scopes.depends_on_scope(output)


def test_output_into_a_temp_table_and_into_a_permanent_table(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "CREATE TABLE #gone (OrderId int NOT NULL);\n"
                       "DELETE o OUTPUT deleted.OrderId INTO #gone (OrderId) FROM sales.[Order] AS o WHERE o.Status = 9;\n"
                       "DELETE FROM sales.[Order] OUTPUT deleted.OrderId, deleted.CustomerId, deleted.OrderDate, "
                       "deleted.Status, deleted.TotalAmount, GETDATE() INTO sales.OrderArchive "
                       "(OrderId, CustomerId, OrderDate, Status, TotalAmount, ArchivedAt) WHERE Status = 8;")

    _, to_temp, to_table = statements(artefact)
    assert to_temp.child("changed_rows").attrs["into_kind"] == "temp_table"
    assert to_temp.child("changed_rows").attrs["into"] == "#gone"
    archive = to_table.child("changed_rows")
    assert (archive.attrs["into_kind"], archive.attrs["into"]) == ("table", "RetailSample.sales.OrderArchive")
    assert "RetailSample.sales.OrderArchive" in artefact.dependencies


def test_output_star_expands_to_the_columns_of_the_target(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "DELETE FROM stg.CustomerImport OUTPUT deleted.* WHERE CustomerCode IS NULL;")

    [delete] = statements(artefact)
    output = delete.child("changed_rows")
    assert len(output.children_with("value")) == len(output.attrs["names"]) > 1


def test_a_merge_outputs_its_action_with_the_rows(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @Changes TABLE (Action nvarchar(10) NOT NULL, Code varchar(20) NULL, OldName nvarchar(100) NULL);\n"
                       "MERGE ref.Customer AS t USING stg.CustomerImport AS s ON t.CustomerCode = s.CustomerCode\n"
                       "WHEN MATCHED THEN UPDATE SET Name = s.Name\n"
                       "WHEN NOT MATCHED THEN INSERT (CustomerCode, Name, Region) VALUES (s.CustomerCode, s.Name, s.Region)\n"
                       "OUTPUT $action, inserted.CustomerCode, deleted.Name INTO @Changes (Action, Code, OldName);")

    _, merge = statements(artefact)
    output = merge.child("changed_rows")
    assert output.children_with("value")[0].kind == "changed_row_action"
    assert output.children_with("value")[0].type.name == "string"
    assert output.attrs["into_kind"] == "row_set"


def test_the_action_is_only_for_a_merge(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "DELETE FROM stg.CustomerImport OUTPUT $action WHERE CustomerCode IS NULL;")

    [delete] = statements(artefact)
    [value] = delete.child("changed_rows").children_with("value")
    assert value.kind == "opaque_fragment" and "$action" in value.attrs["text"]       # kept as written, not dropped


def test_an_output_item_that_reads_a_column_the_target_lacks_is_kept_as_written(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "DELETE FROM stg.CustomerImport OUTPUT deleted.NoSuchColumn "
                                        "WHERE CustomerCode IS NULL;")

    [delete] = statements(artefact)
    [value] = delete.child("changed_rows").children_with("value")
    assert value.kind == "opaque_fragment" and "NoSuchColumn" in value.attrs["text"]


def test_output_into_a_table_variable_after_a_barrier_may_be_stale(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @Ids TABLE (OrderId int NOT NULL);\n"
                       "EXEC('SELECT 1');\n"
                       "UPDATE sales.[Order] SET Status = 3 OUTPUT inserted.OrderId INTO @Ids (OrderId) WHERE Status = 2;")

    *_, update = statements(artefact)
    assert update.kind == "update_rows" and "scope_may_be_stale" in update.child("changed_rows").attrs


def test_an_output_into_something_not_in_scope_is_a_checker_finding(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot,
                            "DECLARE @Ids TABLE (OrderId int NOT NULL);\n"
                            "UPDATE sales.[Order] SET Status = 3 OUTPUT inserted.OrderId INTO @Ids (OrderId);")
    assert checked(artefact, reader) == []
    artefact.body.children = [c for c in artefact.body.children if c.kind != "declare_row_set_variable"]
    assert any("@Ids" in f.message for f in checked(artefact, reader))


# ---------------------------------------------------------------- TOP in a write

def test_top_of_a_delete_limits_the_rows_with_no_order(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "DELETE TOP (10) FROM sales.[Order] WHERE Status = 9;")

    [delete] = statements(artefact)
    limit = delete.child("input")
    assert limit.kind == "limit" and limit.attrs == {"percent": False, "with_ties": False, "ordered": False}
    assert limit.child("count").attrs["value"] == 10
    assert limit.child("input").kind == "filter"            # the rows chosen are among those the WHERE keeps


def test_top_percent_of_an_update(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "UPDATE TOP (25) PERCENT sales.[Order] SET Status = 3 WHERE Status = 2;")

    [update] = statements(artefact)
    limit = update.child("input")
    assert limit.kind == "limit" and limit.attrs["percent"] is True and limit.attrs["ordered"] is False


def test_top_of_an_insert_limits_the_rows_it_reads_whatever_their_order(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "CREATE TABLE #t (Id int NOT NULL);\n"
                       "INSERT TOP (3) INTO #t (Id) SELECT OrderId FROM sales.[Order] ORDER BY OrderDate;")

    _, insert = statements(artefact)
    limit = insert.child("source")
    assert limit.kind == "limit" and limit.attrs["ordered"] is False
    # the ORDER BY of the query does not choose the rows: the plan keeps it, and so does the report of the limit
    assert "sort" in kinds(limit.child("input").walk())


def test_top_of_a_merge_is_a_limit_over_the_rows_the_merge_works_on(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "MERGE TOP (5) PERCENT ref.Customer AS t USING stg.CustomerImport AS s "
                       "ON t.CustomerCode = s.CustomerCode "
                       "WHEN MATCHED THEN UPDATE SET Name = s.Name;")

    [merge] = statements(artefact)
    limit = merge.child("top")
    assert limit.kind == "limit" and limit.attrs == {"percent": True, "with_ties": False, "ordered": False}
    assert limit.child("count").attrs["value"] == 5
    # the rows are those of target and source joined on the condition, as the clauses see them
    rows = limit.child("input")
    assert rows.kind == "join" and rows.attrs["kind"] == "full"
    assert [rows.child("left").kind, rows.child("right").kind, rows.child("condition").kind] == [
        "read_table", "read_table", "compare"]
    assert "top" not in merge.attrs and merge.children_with("top_count") == []


# ---------------------------------------------------------------- INSERT ... EXEC

ORDERS = "RetailSample.sales.uspGetOrdersByCustomer"
ORDERS_SHAPE = [
    {"name": "OrderId", "declared_type": "int", "system_type": "int", "max_length": 4, "precision": 10, "scale": 0,
     "nullable": False},
    {"name": "OrderDate", "declared_type": "datetime", "system_type": "datetime", "max_length": 8, "precision": 23,
     "scale": 3, "nullable": False},
    {"name": "Status", "declared_type": "tinyint", "system_type": "tinyint", "max_length": 1, "precision": 3,
     "scale": 0, "nullable": False},
    {"name": "TotalAmount", "declared_type": "money", "system_type": "money", "max_length": 8, "precision": 19,
     "scale": 4, "nullable": False},
    {"name": "AgeDays", "declared_type": "int", "system_type": "int", "max_length": 4, "precision": 10, "scale": 0,
     "nullable": True}]
FOUND = ("CREATE TABLE #found (OrderId int NULL, OrderDate datetime NULL, Status tinyint NULL, "
         "TotalAmount money NULL, AgeDays int NULL);\n")


def described(snapshot: Snapshot, identity: str = ORDERS, shape=ORDERS_SHAPE) -> Snapshot:
    """The snapshot with the result shape the sandbox would have described for a procedure."""
    p = Provenance("server", "test: a described result shape")
    objects = [CatalogObject(o.identity, o.kind, o.facts + (Fact("skeleton", "result_shape", shape, p),))
               if o.identity == identity else o for o in snapshot.objects]
    return Snapshot.of("test", objects)


def test_insert_exec_takes_the_result_set_of_the_procedure_as_its_rows(corpus_snapshot):
    artefact = lowered(described(corpus_snapshot),
                       FOUND + "INSERT INTO #found (OrderId, OrderDate, Status, TotalAmount, AgeDays) "
                               "EXEC sales.uspGetOrdersByCustomer @CustomerCode = 'A';")

    _, insert = statements(artefact)
    assert insert.kind == "insert_rows" and insert.attrs["target_kind"] == "temp_table"
    call = insert.child("source")
    assert call.kind == "call_procedure" and call.attrs["procedure"] == ORDERS
    assert [c["name"] for c in call.attrs["result_columns"]] == ["OrderId", "OrderDate", "Status", "TotalAmount", "AgeDays"]
    assert ORDERS in artefact.dependencies


def test_insert_exec_without_a_column_list_takes_every_column_of_the_target(corpus_snapshot):
    artefact = lowered(described(corpus_snapshot), FOUND + "INSERT INTO #found EXEC sales.uspGetOrdersByCustomer 'A';")

    _, insert = statements(artefact)
    assert insert.attrs["columns"] == ["OrderId", "OrderDate", "Status", "TotalAmount", "AgeDays"]


def test_insert_exec_of_a_procedure_whose_shape_is_unknown_is_kept_as_written(corpus_snapshot):
    artefact = lowered(corpus_snapshot, FOUND + "INSERT INTO #found EXEC sales.uspGetOrdersByCustomer 'A';")

    kept = statements(artefact)[-1]
    assert kept.kind == "opaque_fragment" and "result set" in kept.attrs["reason"]


def test_insert_exec_may_end_the_transaction_as_a_call_does(corpus_snapshot):
    artefact = lowered(described(corpus_snapshot),
                       FOUND + "BEGIN TRANSACTION;\n"
                               "INSERT INTO #found EXEC sales.uspGetOrdersByCustomer 'A';\nSELECT 1;")

    after = statements(artefact)[-1]
    assert scope_of(after)["transaction"]["min"] == 0 and scope_of(after)["transaction"]["max"] is None


def test_insert_exec_into_a_table_variable_after_a_barrier_may_be_stale(corpus_snapshot):
    artefact = lowered(described(corpus_snapshot),
                       "DECLARE @f TABLE (OrderId int NULL, OrderDate datetime NULL, Status tinyint NULL, "
                       "TotalAmount money NULL, AgeDays int NULL);\n"
                       "EXEC('SELECT 1');\n"
                       "INSERT INTO @f EXEC sales.uspGetOrdersByCustomer 'A';")

    insert = statements(artefact)[-1]
    assert "scope_may_be_stale" in insert.attrs and "scope_may_be_stale" in insert.child("source").attrs


def test_insert_exec_with_a_different_number_of_columns_is_a_checker_finding(corpus_snapshot):
    artefact, reader = proc(described(corpus_snapshot), FOUND + "INSERT INTO #found EXEC sales.uspGetOrdersByCustomer 'A';")
    assert checked(artefact, reader) == []
    [insert] = [n for n in artefact.nodes() if n.kind == "insert_rows"]
    insert.child("source").attrs["result_columns"] = insert.child("source").attrs["result_columns"][:1]
    assert any("columns" in f.message for f in checked(artefact, reader))


# ---------------------------------------------------------------- global temp tables

def test_one_helper_names_a_global_temp_table():
    assert scopes.is_global_temp_table("##g") and not scopes.is_global_temp_table("#t")
    assert not scopes.is_global_temp_table("t")


def test_a_global_temp_table_is_lowered_reported_and_not_private(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "CREATE TABLE ##g (Id int NOT NULL, Name varchar(10) NULL);\n"
                       "INSERT INTO ##g (Id, Name) VALUES (1, 'a');\n"
                       "SELECT g.Id FROM ##g AS g;\n"
                       "DROP TABLE ##g;")

    create, insert, emit, drop = statements(artefact)
    assert create.kind == "create_global_temp_table" and create.attrs["name"] == "##g"
    assert create.attrs["private"] is False and create.attrs["reported"] is True
    assert [c["name"] for c in create.attrs["columns"]] == ["Id", "Name"]
    assert insert.kind == "insert_rows" and (insert.attrs["target"], insert.attrs["target_kind"]) == (
        "##g", "global_temp_table")
    [read] = [n for n in emit.walk() if n.kind == "read_table"]
    assert read.attrs["global_temp_table"] == "##g" and "temp_table" not in read.attrs
    assert drop.kind == "drop_temp_table" and drop.attrs["name"] == "##g"


def test_the_scope_lists_global_temp_tables_apart_from_private_ones(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "CREATE TABLE #p (Id int NOT NULL);\n"
                       "CREATE TABLE ##g (Id int NOT NULL);\n"
                       "SELECT 1;\n"
                       "DROP TABLE ##g;\n"
                       "SELECT 2;")

    _, _, first, _, last = statements(artefact)
    assert scope_of(first)["temp_tables"] == ["#p"] and scope_of(first)["global_temp_tables"] == ["##g"]
    assert scope_of(last)["temp_tables"] == ["#p"] and scope_of(last)["global_temp_tables"] == []


def test_the_source_half_says_only_how_sql_server_behaves_of_a_global_temp_table(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "CREATE TABLE ##g (Id int NOT NULL);")

    [create] = statements(artefact)
    # how the target treats it, and the caveat that goes with that, are the target half's to say (ADR 0019)
    assert not {"generation", "caveat", "treatment"} & set(create.attrs)
    meaning = next(o for g in vocabulary._data()["groups"] for o in g["operations"]
                   if o["name"] == "create_global_temp_table")["meaning"]
    assert "scratch" not in meaning and "reproduced" not in meaning and "every session" in meaning


def test_the_target_half_says_how_a_global_temp_table_is_treated_and_what_is_not_reproduced():
    from migrt.targets.pyspark import global_temp

    assert "scratch table" in global_temp.TREATMENT and "run" in global_temp.TREATMENT
    assert "cross-session visibility is not reproduced" in global_temp.CAVEAT


def test_select_into_a_global_temp_table(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "SELECT o.OrderId INTO ##ids FROM sales.[Order] AS o;\nSELECT i.OrderId FROM ##ids AS i;")

    create, emit = statements(artefact)
    assert create.kind == "create_global_temp_table" and create.attrs["from_query"] is True
    assert create.child("query").kind == "project"


def test_a_read_of_a_global_temp_table_the_artefact_never_creates_is_manual_work(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "SELECT g.Id FROM ##made_elsewhere AS g;")

    [emit] = statements(artefact)
    [read] = [n for n in emit.walk() if n.kind == "external_reference"]
    assert read.attrs["global_temp_table"] == "##made_elsewhere" and read.attrs["manual"] is True
    assert "another session" in read.attrs["reason"]


def test_a_write_to_a_global_temp_table_the_artefact_never_creates_is_kept_as_written(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "INSERT INTO ##made_elsewhere (Id) VALUES (1);")

    [kept] = statements(artefact)
    assert kept.kind == "opaque_fragment" and "another session" in kept.attrs["reason"]


def test_a_read_of_a_global_temp_table_not_in_scope_and_not_marked_manual_is_a_checker_finding(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, "CREATE TABLE ##g (Id int NOT NULL);\nSELECT g.Id FROM ##g AS g;")
    assert checked(artefact, reader) == []
    artefact.body.children = [c for c in artefact.body.children if c.kind != "create_global_temp_table"]
    assert any("##g" in f.message for f in checked(artefact, reader))


def test_a_global_temp_table_made_in_a_branch_may_exist_after_it(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @c int = 1;\nIF @c = 1 CREATE TABLE ##maybe (Id int NOT NULL);\nSELECT 1;")

    last = statements(artefact)[-1]
    assert scope_of(last)["global_temp_tables"] == ["##maybe"]


def test_a_rollback_undoes_the_creation_of_a_global_temp_table_as_it_does_a_private_one(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "BEGIN TRANSACTION;\nCREATE TABLE ##g (Id int NOT NULL);\nROLLBACK TRANSACTION;\nSELECT 1;")

    assert scope_of(statements(artefact)[-1])["global_temp_tables"] == []


def test_global_temp_tables_survive_go_in_a_script(corpus_snapshot):
    artefact = lowered_script(corpus_snapshot,
                              "CREATE TABLE ##g (Id int NOT NULL);\nGO\nSELECT g.Id FROM ##g AS g;\n")

    second = artefact.body.children_with("batch")[1]
    assert second.children_with("statement")[0].kind == "emit_result_set"


def test_the_report_lists_global_temp_tables_apart_from_private_ones(corpus_snapshot):
    from migrt.core import report

    artefact = lowered(corpus_snapshot,
                       "CREATE TABLE #p (Id int NOT NULL);\n"
                       "CREATE TABLE ##g (Id int NOT NULL);\n"
                       "SELECT g.Id FROM ##g AS g;\n"
                       "SELECT e.Id FROM ##elsewhere AS e;")

    listed = report.global_temp_tables(artefact.body, treatment="a scratch table", caveat="not reproduced")
    assert [(g["name"], g["created_in_artefact"]) for g in listed] == [("##g", True), ("##elsewhere", False)]
    assert listed[0]["caveat"] == "not reproduced"
    assert listed[1]["treatment"] == "manual work" and "another session" in listed[1]["text"]
    assert listed[0]["treatment"] == "a scratch table"
    assert all(g["name"] != "#p" for g in listed)


def test_the_target_reports_a_read_of_a_global_temp_table_made_elsewhere_as_manual_work(corpus_snapshot):
    from migrt.targets.pyspark import generator, untranslatable

    artefact = lowered(corpus_snapshot, "SELECT g.Id FROM ##made_elsewhere AS g;")

    findings = untranslatable.detect(artefact, generator.handled_kinds())
    [found] = [f for f in findings if f.kind == "external_reference"]
    assert "another session" in found.reason


# ---------------------------------------------------------------- the corpus

def test_the_corpus_procedures_that_merge_and_output_are_lowered_not_kept_as_written(corpus_snapshot):
    from test_procedural_lowering import lower_module

    found = {}
    for identity in ("RetailSample.ref.uspMergeCustomerImport", "RetailSample.sales.uspApplyPriceImport",
                     "RetailSample.sales.uspArchiveOrders"):
        artefact, reader = lower_module(corpus_snapshot, corpus_snapshot.get(identity))
        assert checked(artefact, reader) == [], identity
        found[identity] = {n.kind for n in artefact.nodes()}
        statements_kept = [n for n in artefact.nodes() if n.kind == "opaque_fragment" and n.role == "statement"]
        assert statements_kept == [], (identity, [n.attrs["reason"] for n in statements_kept])
    assert "merge_rows" in found["RetailSample.ref.uspMergeCustomerImport"]
    assert {"merge_rows", "return_changed_rows", "changed_row_action"} <= found["RetailSample.sales.uspApplyPriceImport"]
    assert "return_changed_rows" in found["RetailSample.sales.uspArchiveOrders"]


# ---------------------------------------------------------------- database (DDL) triggers

def database_trigger_snapshot(text: str, secrets=()):
    p = Provenance("server", "test: a database trigger")
    trigger = CatalogObject("RetailSample.trNoDrops", "database_trigger", (
        Fact("skeleton", "events", ["DROP_TABLE", "ALTER_TABLE"], p), Fact("skeleton", "timing", "AFTER", p),
        Fact("skeleton", "firing_order", {"ALTER_TABLE": "none", "DROP_TABLE": "first"}, p),
        Fact("skeleton", "enabled", True, p), Fact("skeleton", "source_text", text, p),
        Fact("skeleton", "extractable", True, p), Fact("skeleton", "secrets", list(secrets), p)))
    return Snapshot.of("test", [trigger])


def test_a_database_trigger_is_reported_as_manual_work_with_its_events_and_text():
    text = "CREATE TRIGGER trNoDrops ON DATABASE FOR DROP_TABLE, ALTER_TABLE AS ROLLBACK;"

    [found] = extractor.recognised(database_trigger_snapshot(text))

    assert found["name"] == "RetailSample.trNoDrops" and found["type"] == "Database (DDL) trigger"
    assert found["inventory"]["events"] == ["DROP_TABLE", "ALTER_TABLE"]
    assert found["inventory"]["timing"] == "AFTER" and found["inventory"]["enabled"] is True
    assert found["inventory"]["source_text"] == text          # nothing is dropped


def test_a_database_triggers_text_has_its_marked_secrets_masked():
    text = "CREATE TRIGGER trNoDrops ON DATABASE FOR DROP_TABLE AS EXEC xp 'Password=Hunter2!';"
    start = text.index("Hunter2!")
    marked = [{"fact": "source_text", "row": None, "column": None, "start": start, "length": len("Hunter2!"),
               "pattern": "p"}]

    [found] = extractor.recognised(database_trigger_snapshot(text, marked))

    assert "Hunter2!" not in found["inventory"]["source_text"] and "[secret: setting" in found["inventory"]["source_text"]
    assert len(found["secrets"]) == 1
