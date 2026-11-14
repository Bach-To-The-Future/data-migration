"""Full live extraction from SQL Server 2017 and later (issue #39, spec section 3), through
the engine's library interface, on the sandbox: the scratch database (conftest) for what the
corpus lacks, and RetailSample for the corpus."""
import copy as copy_module
import json
import pathlib
import re

import pytest

from migrt import engine
from migrt.core.secrets import SecretSpan
from migrt.sources.sqlserver import extractor
from migrt.sources.sqlserver.connection import Connection, SqlServerError

from conftest import SCRATCH, sa_connection, sandbox_server


@pytest.fixture
def scratch(scratch_database):
    return sandbox_server(databases=(scratch_database,))


def facts(snapshot, identity):
    return {f.name: f.value for f in snapshot.get(identity).facts}


# ---------------------------------------------------------------- every catalog level

def test_the_server_and_the_database_levels(engagement, scratch):
    snapshot = engine.extract(engagement, scratch)

    [server] = [o for o in snapshot.objects if o.kind == "server"]
    s = facts(snapshot, server.identity)
    assert s["version"].startswith("16.") and s["edition"] and s["collation"] == "SQL_Latin1_General_CP1_CI_AS"
    assert s["build"]["release"] == "2022" and s["build"]["update_level"] == "CU27"
    assert s["time_zone"] and s["nested_triggers"] is True
    db = facts(snapshot, f"{SCRATCH}")
    assert db["collation"] == "Latin1_General_CS_AS"
    assert db["compatibility_level"] == 160 and db["recursive_triggers"] is False
    assert set(db["ansi_defaults"]) == {"ansi_nulls", "ansi_padding", "ansi_warnings", "arithabort",
                                        "concat_null_yields_null", "numeric_roundabort", "quoted_identifier",
                                        "ansi_null_default"}


def test_a_table_with_its_columns_constraints_and_row_count(engagement, scratch):
    snapshot = engine.extract(engagement, scratch)

    parent = facts(snapshot, f"{SCRATCH}.s.Parent")
    columns = {c["name"]: c for c in parent["columns"]}
    assert columns["Id"]["identity"] and (columns["Id"]["identity_seed"], columns["Id"]["identity_increment"]) == (10, 2)
    assert columns["Code"]["declared_type"] == "Code" and columns["Code"]["system_type"] == "nvarchar"
    assert columns["Code"]["user_type"] == f"type:{SCRATCH}.dbo.Code" and not columns["Code"]["nullable"]
    assert columns["Note"]["collation"] == "Latin1_General_CI_AS"
    assert columns["Note"]["default"] == {"name": "DF_Parent_Note", "definition": "('none')"}
    assert columns["Twice"]["computed_expression"] == "([Id]*(2))" and columns["Twice"]["computed_persisted"]
    assert parent["not_null"] == ["Id", "Code"]
    assert parent["primary_key"]["name"] == "PK_Parent" and parent["primary_key"]["columns"] == [
        {"name": "Id", "descending": False}]
    assert [u["name"] for u in parent["unique_constraints"]] == ["UQ_Parent_Code"]
    # SQL Server records the column of a table-level check that names exactly one column.
    assert parent["check_constraints"] == [{"name": "CK_Parent_Note", "definition": "([Note]<>'bad')", "column": "Note",
                                            "enabled": True, "trusted": True}]
    assert parent["row_count"] == 0

    child = facts(snapshot, f"{SCRATCH}.s.Child")
    [fk] = child["foreign_keys"]
    assert (fk["columns"], fk["references"], fk["referenced_columns"], fk["on_delete"], fk["on_update"]) == (
        ["ParentId"], f"{SCRATCH}.s.Parent", ["Id"], "CASCADE", "NO_ACTION")
    [ux] = child["unique_indexes"]
    assert (ux["name"], ux["included"], ux["filter"]) == ("UX_Child_Email", ["ParentId"], "([Email] IS NOT NULL)")


def test_row_counts_come_from_metadata(engagement, extractor_server):
    snapshot = engine.extract(engagement, extractor_server)

    count = facts(snapshot, "RetailSample.sales.Order")["row_count"]
    assert count > 0
    assert snapshot.get("RetailSample.sales.Order").fact("row_count").provenance.detail.startswith("sys.partitions")


def test_routines_and_views_with_parameters_shapes_and_settings(engagement, scratch):
    snapshot = engine.extract(engagement, scratch)

    add = facts(snapshot, f"{SCRATCH}.s.uspAdd")
    assert [(p["name"], p["declared_type"], p["direction"], p["default"]) for p in add["parameters"]] == [
        ("@code", "Code", "input", None), ("@note", "varchar", "input", "'n/a'"), ("@id", "int", "output", None)]
    assert add["ansi_nulls"] is True and add["quoted_identifier"] is True and add["extractable"] is True
    assert "result_shape" not in add   # described in the sandbox (issue #41), never read with EXECUTE
    double = facts(snapshot, f"{SCRATCH}.s.fnDouble")
    assert double["returns"]["declared_type"] == "int" and double["parameters"][0]["default"] == "4"
    assert [c["name"] for c in facts(snapshot, f"{SCRATCH}.s.fnChildren")["result_shape"]] == ["Id"]
    assert [c["name"] for c in facts(snapshot, f"{SCRATCH}.s.vParents")["result_shape"]] == ["Id", "Twice"]
    assert snapshot.get(f"{SCRATCH}.s.fnChildren").kind == "inline_function"


def test_an_encrypted_routine_is_present_but_not_extractable(engagement, scratch):
    snapshot = engine.extract(engagement, scratch)

    hidden = facts(snapshot, f"{SCRATCH}.s.uspHidden")
    assert hidden["extractable"] is False and hidden["source_text"] is None
    assert "encrypted" in hidden["not_extractable_reason"]
    # Its parameter's default is in the text it hides: unknown, never recorded as none.
    [p] = hidden["parameters"]
    assert "default" not in p and "not extractable" in p["default_unknown"]


def test_triggers_with_their_table_events_timing_order_and_state(engagement, scratch):
    snapshot = engine.extract(engagement, scratch)

    a, b = facts(snapshot, f"{SCRATCH}.s.trParentA"), facts(snapshot, f"{SCRATCH}.s.trParentB")
    assert a["table"] == f"{SCRATCH}.s.Parent" and a["events"] == ["INSERT", "UPDATE"]
    assert a["timing"] == "AFTER" and a["enabled"] is True
    assert b["firing_order"] == {"INSERT": "first"} and a["firing_order"] == {"INSERT": "none", "UPDATE": "none"}
    child = facts(snapshot, f"{SCRATCH}.s.trChild")
    assert child["timing"] == "INSTEAD OF" and child["enabled"] is False and child["events"] == ["INSERT"]
    ddl = facts(snapshot, f"{SCRATCH}.trDdl")
    assert snapshot.get(f"{SCRATCH}.trDdl").kind == "database_trigger" and ddl["events"] == ["CREATE_TABLE"]


def test_synonyms_types_and_sequences(engagement, scratch):
    snapshot = engine.extract(engagement, scratch)

    assert facts(snapshot, f"{SCRATCH}.dbo.Parents")["base_object"] == ["s", "Parent"]
    assert facts(snapshot, f"{SCRATCH}.dbo.Remote")["base_object"] == ["OtherDb", "dbo", "Thing"]
    code = facts(snapshot, f"type:{SCRATCH}.dbo.Code")["base_type"]
    assert (code["system_type"], code["max_length"], code["nullable"]) == ("nvarchar", 20, False)
    lines = facts(snapshot, f"type:{SCRATCH}.s.Lines")
    assert [c["name"] for c in lines["columns"]] == ["Id", "Qty"] and lines["primary_key"] is not None
    assert len(lines["check_constraints"]) == 1
    seq = facts(snapshot, f"{SCRATCH}.s.Seq")["sequence"]
    assert (seq["type"], seq["start"], seq["increment"]) == ("bigint", 100, 5)


def test_sql_agent_jobs_from_msdb(engagement, extractor_server):
    snapshot = engine.extract(engagement, extractor_server)

    [job] = [o for o in snapshot.objects if o.kind == "agent_job"]
    j = facts(snapshot, job.identity)
    assert job.identity == "agent_job:RetailSample - Nightly sales roll-up"
    assert j["steps"] and all(s["command"] and s["subsystem"] for s in j["steps"])
    assert j["schedules"] and j["category"]


# ---------------------------------------------------------------- the raw capture

def test_the_raw_capture_holds_every_column_of_each_view_read_and_no_other_views(engagement, scratch):
    snapshot = engine.extract(engagement, scratch)

    raw = {o.identity: facts(snapshot, o.identity) for o in snapshot.objects if o.kind == "raw_capture"}
    expected = ({extractor.raw_identity("server", v) for v in extractor.SERVER_VIEWS} |
                {extractor.raw_identity("db:msdb", v) for v in extractor.MSDB_VIEWS} |
                {extractor.raw_identity(f"db:{SCRATCH}", v)
                 for v in extractor.DATABASE_VIEWS + (extractor.DEPENDENCY_VIEW,)})
    assert set(raw) == expected
    with sa_connection(f"{SCRATCH}") as sa:
        for identity, capture in raw.items():
            database = "msdb" if identity.startswith("raw:db:msdb/") else f"{SCRATCH}"
            every = [r["name"] for r in sa.query(f"USE [{database}]; SELECT name FROM sys.all_columns "
                                                 "WHERE object_id = OBJECT_ID(@view) ORDER BY column_id",
                                                 {"view": capture["view"]})]
            assert capture["columns"] == every, identity
            assert all(list(row) == every for row in capture["rows"]), identity
    assert len(raw[extractor.raw_identity(f"db:{SCRATCH}", "sys.databases")]["rows"]) == 1


def test_the_extractor_sends_only_metadata_reads(scratch_database):
    sent: list[str] = []
    source = sandbox_server(databases=(scratch_database,))
    extractor.extract(extractor.ServerAddress(source.host, source.port, source.login, source.password),
                      [scratch_database], statements=sent)

    views = set(extractor.SERVER_VIEWS + extractor.DATABASE_VIEWS + extractor.MSDB_VIEWS + (extractor.DEPENDENCY_VIEW,))
    # Only sys.databases is filtered, to the database being read (ADR 0024).
    captures = [re.fullmatch(r"SELECT \* FROM (sys\.databases WHERE database_id = DB_ID\(\)|(?!sys\.databases)\S+)", s)
                for s in sent]
    others = [s for s, m in zip(sent, captures) if m is None]
    assert {m.group(1).split(" ")[0] for m in captures if m} == views
    # Everything else is the version check, the self-check, CURRENT_TIMEZONE() and HAS_PERMS_BY_NAME.
    assert all(re.search(r"SERVERPROPERTY|fn_my_permissions|HAS_DBACCESS|HAS_PERMS_BY_NAME|IS_ROLEMEMBER|"
                         r"CURRENT_TIMEZONE", s) for s in others), others
    # None runs or changes anything: no statement keyword outside a string literal (the
    # self-check names permissions such as 'EXECUTE' as literals).
    code = [re.sub(r"N?'(?:[^']|'')*'", "''", s) for s in others]
    assert not any(re.search(r"\b(EXEC|EXECUTE|INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|GRANT)\b", c, re.I)
                   for c in code), code


# ---------------------------------------------------------------- secrets

def test_likely_secrets_are_marked_in_source_text_and_raw_rows(engagement, scratch):
    snapshot = engine.extract(engagement, scratch)

    link = facts(snapshot, f"{SCRATCH}.s.uspLink")
    text = link["source_text"]
    assert [text[s["start"]:s["start"] + s["length"]] for s in link["secrets"]] == ["loader", "Hunter2!"]
    modules = facts(snapshot, extractor.raw_identity(f"db:{SCRATCH}", "sys.sql_modules"))
    marked = [SecretSpan.from_json(s) for s in modules["secrets"]]
    values = {modules["rows"][s.row][s.column][s.start:s.end] for s in marked}
    assert {"loader", "Hunter2!"} <= values


def test_a_secret_is_masked_in_the_report_never_written_into_generated_code(engagement, scratch, tmp_path):
    result = engine.preview_run(engagement, scratch, artefacts=[f"{SCRATCH}.s.vSecret"], out_dir=tmp_path)

    [artefact] = result.artefacts
    code = artefact.file.read_text(encoding="utf-8")
    report = result.report_data_path.read_text(encoding="utf-8")
    for secret in ("Hunter2!", "loader"):
        assert secret not in code and secret not in report
    assert f"[secret: setting {SCRATCH}.s.vSecret.secret_1]" in code
    data = json.loads(report)
    [item] = data["artefacts"]
    assert [s["setting"] for s in item["secrets"]] == [f"{SCRATCH}.s.vSecret.secret_1",
                                                       f"{SCRATCH}.s.vSecret.secret_2"]
    assert "miss some" in data["extraction"]["secrets"]["note"]


# ---------------------------------------------------------------- references

def test_references_are_worked_out_by_the_parser_and_resolved(engagement, scratch):
    snapshot = engine.extract(engagement, scratch)

    add = {tuple(r["name"]): r for r in facts(snapshot, f"{SCRATCH}.s.uspAdd")["references"]}
    assert add[("s", "Parent")]["resolved"] == f"{SCRATCH}.s.Parent"
    assert add[("dbo", "Code")]["resolved"] == f"type:{SCRATCH}.dbo.Code"
    assert add[("s", "Seq")]["resolved"] == f"{SCRATCH}.s.Seq"
    assert add[("s", "uspLog")]["resolved"] is None and add[("s", "uspLog")]["external"] == "not in the catalog"
    caller = {tuple(r["name"]): r["resolved"] for r in facts(snapshot, f"{SCRATCH}.s.uspCaller")["references"]}
    assert caller == {("s", "fnDouble"): f"{SCRATCH}.s.fnDouble",
                      ("s", "fnChildren"): f"{SCRATCH}.s.fnChildren"}
    # A synonym resolves to its base object; a trigger's inserted and deleted are not references.
    [via] = facts(snapshot, f"{SCRATCH}.s.vParents")["references"]
    assert (via["resolved"], via["via"]) == (f"{SCRATCH}.s.Parent", f"{SCRATCH}.dbo.Parents")
    # SQL Server records the synonym, which is what the name was written as: no disagreement.
    assert facts(snapshot, f"{SCRATCH}.s.vParents")["dependency_cross_check"]["status"] == "agrees"
    assert facts(snapshot, f"{SCRATCH}.s.trChild")["references"] == []


def test_sql_servers_dependency_metadata_is_a_cross_check_with_disagreements_reported(engagement, scratch, tmp_path):
    result = engine.preview_run(engagement, scratch, artefacts=[f"{SCRATCH}.s.vParents"], out_dir=tmp_path)
    snapshot = engine.read_snapshot(engagement, result.snapshot_id)

    add = facts(snapshot, f"{SCRATCH}.s.uspAdd")
    assert add["server_dependencies"] and add["dependency_cross_check"]["status"] in ("agrees", "disagrees")
    checks = {o.identity: facts(snapshot, o.identity)["dependency_cross_check"] for o in snapshot.objects
              if "dependency_cross_check" in facts(snapshot, o.identity) and o.kind != "database"}
    data = json.loads(result.report_data_path.read_text(encoding="utf-8"))
    reported = data["extraction"]["dependency_cross_check"]
    assert {d["object"] for d in reported["disagreements"]} == {i for i, c in checks.items()
                                                                if c["status"] == "disagrees"}
    assert {d["object"] for d in reported["not_run"]} == {i for i, c in checks.items() if c["status"] == "not run"}
    assert reported["databases"][f"{SCRATCH}"] == {"status": "run"}


def test_what_one_side_never_records_is_set_aside_and_listed_never_dropped(engagement, extractor_server):
    snapshot = engine.extract(engagement, extractor_server)

    trigger = facts(snapshot, "RetailSample.sales.trOrder_AuditStatus")["dependency_cross_check"]
    assert trigger["status"] == "agrees"
    assert trigger["ignored_server"]["entries"] == ["name:deleted", "name:inserted"]
    search = facts(snapshot, "RetailSample.sales.uspSearchOrders")["dependency_cross_check"]
    assert search["ignored_parser"]["entries"] == ["name:sys.sp_executesql"]


def test_a_real_disagreement_on_the_public_corpus_is_reported(engagement, tmp_path):
    """SQL Server records an XML method call, CatalogDescription.value(...), as a dependency;
    the parser does not take a method on a column for an object."""
    source = sandbox_server(databases=("AdventureWorks2022",))
    result = engine.preview_run(engagement, source, artefacts=["AdventureWorks2022.Production.vProductAndDescription"],
                                out_dir=tmp_path)

    reported = json.loads(result.report_data_path.read_text(encoding="utf-8"))["extraction"]["dependency_cross_check"]
    assert {"object": "AdventureWorks2022.Production.vProductModelCatalogDescription", "parser_only": [],
            "server_only": ["name:catalogdescription.value"]} in reported["disagreements"]


def test_without_the_optional_grant_the_cross_check_is_skipped_and_reported(engagement, scratch_database, tmp_path):
    with sa_connection(scratch_database) as sa:
        sa.query("REVOKE SELECT ON sys.sql_expression_dependencies FROM [migration_extractor]")
    try:
        result = engine.preview_run(engagement, sandbox_server(databases=(scratch_database,)),
                                    artefacts=[f"{SCRATCH}.s.vParents"], out_dir=tmp_path)
    finally:
        with sa_connection(scratch_database) as sa:
            sa.query("GRANT SELECT ON sys.sql_expression_dependencies TO [migration_extractor]")

    data = json.loads(result.report_data_path.read_text(encoding="utf-8"))
    assert data["extraction"]["dependency_cross_check"]["databases"][f"{SCRATCH}"]["status"] == "skipped"
    assert any("Dependency cross-check skipped" in d["text"] for d in data["degradations"])


# ---------------------------------------------------------------- hashes

def test_changing_one_table_changes_only_that_tables_hash_and_the_raw_capture(engagement, scratch, scratch_database):
    before = engine.extract(engagement, scratch)
    with sa_connection(scratch_database) as sa:
        sa.query("ALTER TABLE s.Child ADD Extra int NULL")
    try:
        after = engine.extract(engagement, scratch)
    finally:
        with sa_connection(scratch_database) as sa:
            sa.query("ALTER TABLE s.Child DROP COLUMN Extra")

    changed = {o.identity for o in after.objects if o.content_hash != before.get(o.identity).content_hash}
    assert f"{SCRATCH}.s.Child" in changed
    assert {i for i in changed if not i.startswith("raw:")} == {f"{SCRATCH}.s.Child"}
    assert after.id != before.id


ADR_0023 = pathlib.Path(__file__).parents[2] / "docs" / "adr" / "0023-a-snapshots-identifier-is-the-hash-of-its-content.md"


def _adr_table(header: str) -> dict[str, tuple[str, ...]]:
    """The rows of the ADR 0023 amendment's table whose header row starts with `header`."""
    text = ADR_0023.read_text(encoding="utf-8")
    table = text[text.index(f"| {header} |"):].split("\n\n")[0]
    rows = re.findall(r"^\| `([^`]+)` \| (.+) \|$", table, re.M)
    return {key: tuple(re.findall(r"`([^`]+)`", cells)) for key, cells in rows}


def test_what_the_identity_leaves_out_is_exactly_what_the_recorded_decision_lists():
    """These lists change only by a recorded decision: ADR 0023's amendment holds them."""
    assert _adr_table("View") == extractor.IGNORED_FOR_IDENTITY
    assert _adr_table("Object kind") == extractor.INFORMATIONAL_FACTS


def test_nothing_read_by_conversion_tests_or_validation_comes_from_what_the_identity_leaves_out(scratch_database):
    """Every fact outside the ignored list is unchanged when every ignored raw cell changes,
    and so is the snapshot; the informational row counts built from them do change. The
    control changes one cell that is not ignored."""
    source = sandbox_server(databases=(scratch_database, "RetailSample"))
    reading = extractor.read(extractor.ServerAddress(source.host, source.port, source.login, source.password),
                             [scratch_database, "RetailSample"])

    def perturbed(change):
        copy = copy_module.deepcopy(reading)
        captures = [*copy.server.values(), *copy.msdb.values(),
                    *(c for caps in copy.database_captures.values() for c in caps.values())]
        touched = 0
        for cap in captures:
            for row in cap.rows:
                for column in change.get(cap.view, ()):
                    row[column] = 10 ** 9 + touched if isinstance(row[column], int) else f"changed {row[column]!r}"
                    touched += 1
        return extractor.build(copy), touched

    def readable(snapshot):
        """Every fact of every object, less what its identity leaves out."""
        return {o.identity: {(f.area, f.name): f.value for f in o.facts if f.name not in o.ignored_facts()}
                for o in snapshot.objects}

    base = extractor.build(reading)
    moved, touched = perturbed(extractor.IGNORED_FOR_IDENTITY)
    assert touched > 0
    assert moved.id == base.id
    assert readable(moved) == readable(base)
    assert facts(moved, "RetailSample.sales.Order")["row_count"] != facts(base, "RetailSample.sales.Order")["row_count"]
    assert "row_count" in moved.get("RetailSample.sales.Order").ignored_facts()

    control, _ = perturbed({"sys.databases": ("is_recursive_triggers_on",)})
    assert control.id != base.id
    assert readable(control) != readable(base)


def test_only_the_report_reads_informational_facts():
    """catalog.report_value is the one way to read a fact the identity leaves out."""
    package = pathlib.Path(extractor.__file__).parents[2]
    callers = {p.relative_to(package).as_posix() for p in package.rglob("*.py")
               if "report_value(" in p.read_text(encoding="utf-8")}
    assert callers <= {"core/catalog.py", "core/report.py"}


def test_re_extracting_an_unchanged_server_invalidates_nothing(engagement, scratch):
    first = engine.extract(engagement, scratch)
    second = engine.extract(engagement, scratch)

    assert first.id == second.id
    assert [(o.identity, o.content_hash) for o in first.objects if o.kind != "raw_capture"] == [
        (o.identity, o.content_hash) for o in second.objects if o.kind != "raw_capture"]
    # Raw captures differ, if at all, only where SQL Server changed its own metadata meanwhile.
    for o in first.objects:
        if o.kind == "raw_capture" and o.content_hash != second.get(o.identity).content_hash:
            assert o.identity_hash == second.get(o.identity).identity_hash, o.identity


# ---------------------------------------------------------------- name adjustments

def test_name_adjustments_are_recorded_once_and_kept_by_later_extractions(engagement, scratch, scratch_database):
    engine.extract(engagement, scratch)
    first = engine.name_adjustments(engagement)
    with sa_connection(scratch_database) as sa:
        sa.query("CREATE TABLE s.totals (Id int)")
    try:
        engine.extract(engagement, scratch)
    finally:
        with sa_connection(scratch_database) as sa:
            sa.query("DROP TABLE s.totals")
    later = engine.name_adjustments(engagement)

    tables = first[("unity-catalog", f"{SCRATCH}.s")]
    assert tables["Order Lines"] == "Order_Lines"
    assert (tables["TOTALS"], tables["Totals"]) == ("TOTALS", "Totals_2")
    assert first[("python-module", f"{SCRATCH}.s.views")]["v Order Lines"] == "v_Order_Lines"
    assert later[("unity-catalog", f"{SCRATCH}.s")] == {**tables, "totals": "totals_3"}


def test_generated_code_uses_the_recorded_names(engagement, scratch, tmp_path):
    result = engine.preview_run(engagement, scratch, artefacts=[f"{SCRATCH}.s.v Order Lines"], out_dir=tmp_path)

    [artefact] = result.artefacts
    assert artefact.file == tmp_path / "generated" / f"{SCRATCH}" / "s" / "views" / "v_Order_Lines.py"
    code = artefact.file.read_text(encoding="utf-8")
    assert "def v_Order_Lines(ss)" in code and f'spark.read.table("`{SCRATCH}`.`s`.`Order_Lines`")' in code
    assert artefact.layer1.passed, artefact.layer1.failures
    [item] = json.loads(result.report_data_path.read_text(encoding="utf-8"))["artefacts"]
    assert item["name_adjustments"] == {"unity-catalog": {f"{SCRATCH}.s.Order Lines": "Order_Lines"},
                                        "python-module": {f"{SCRATCH}.s.views.v Order Lines": "v_Order_Lines"}}


# ---------------------------------------------------------------- the extractor's promises

@pytest.mark.parametrize("statement, error", [
    ("SELECT TOP 1 * FROM sales.[Order]", 229),
    ("EXEC sales.uspGetOrdersByCustomer @CustomerId = 1", 229),
    ("INSERT INTO sales.[Order] (CustomerId) VALUES (1)", 229),
    ("UPDATE sales.[Order] SET Note = 'x'", 229),
    ("DELETE FROM sales.[Order]", 229),
    ("CREATE TABLE dbo.Planted (Id int)", 262),
])
def test_the_extractors_login_cannot_read_rows_execute_or_write(statement, error):
    source = sandbox_server()
    with Connection(source.host, source.port, source.login, source.password, "RetailSample") as conn:
        with pytest.raises(SqlServerError) as refused:
            conn.query(statement)
    assert error in refused.value.numbers


def test_a_grant_that_advances_a_sequence_is_refused(engagement, scratch, scratch_database):
    """UPDATE on a sequence lets NEXT VALUE FOR advance it: a write."""
    with sa_connection(scratch_database) as sa:
        sa.query("GRANT UPDATE ON s.Seq TO [migration_extractor]")
    try:
        with pytest.raises(engine.ExtractorPermissionError) as raised:
            engine.extract(engagement, scratch)
    finally:
        with sa_connection(scratch_database) as sa:
            sa.query("REVOKE UPDATE ON s.Seq FROM [migration_extractor]")
    assert f"database {SCRATCH}: UPDATE on [s].[Seq]" in raised.value.excess


def test_insert_reported_on_a_history_table_is_refused_by_sql_server_and_recorded(engagement, tmp_path):
    """SQL Server reports INSERT on WideWorldImporters' temporal history tables to a login
    granted nothing there, and refuses it (ADR 0024). The extraction goes ahead and says so."""
    source = sandbox_server(databases=("WideWorldImporters",))
    with Connection(source.host, source.port, source.login, source.password, "WideWorldImporters") as conn:
        with pytest.raises(SqlServerError) as refused:
            conn.query("BEGIN TRAN; INSERT INTO Application.Cities_Archive (CityID) VALUES (1); ROLLBACK")
    assert 13559 in refused.value.numbers

    result = engine.preview_run(engagement, source, artefacts=["WideWorldImporters.Website.Customers"],
                                out_dir=tmp_path)

    exempted = json.loads(result.report_data_path.read_text(encoding="utf-8"))["extraction"]["permission_exemptions"]
    entries = exempted["WideWorldImporters"]
    assert {"object": "[Application].[Cities_Archive]", "permission": "INSERT",
            "reason": extractor.HISTORY_TABLE_EXEMPTION} in entries
    assert all(e["object"].endswith("_Archive]") and e["permission"] == "INSERT" for e in entries)


def test_row_derived_counters_stay_in_the_raw_capture_only(engagement, scratch):
    """Like row counts, an identity's last value and a sequence's current value are metadata
    (decided on issue #39, ADR 0024): captured wholesale, never in a fact conversion reads."""
    snapshot = engine.extract(engagement, scratch)

    raw = facts(snapshot, extractor.raw_identity(f"db:{SCRATCH}", "sys.sequences"))
    assert "current_value" in raw["columns"]
    assert "current_value" not in facts(snapshot, f"{SCRATCH}.s.Seq")["sequence"]
    assert not any("last_value" in c for c in facts(snapshot, f"{SCRATCH}.s.Parent")["columns"])


def test_inserting_rows_and_drawing_sequence_values_leaves_the_snapshot_unchanged(engagement, scratch,
                                                                                  scratch_database):
    """The counters move with the client's rows, not its objects (ADR 0023, amended): the
    identifier leaves them out, and the extraction records the raw captures that differed."""
    before = engine.extract(engagement, scratch)
    with sa_connection(scratch_database) as sa:
        sa.query("INSERT INTO s.Parent (Code) VALUES (N'counter')")
        sa.query("SELECT NEXT VALUE FOR s.Seq")
    try:
        after = engine.extract(engagement, scratch)
    finally:
        with sa_connection(scratch_database) as sa:
            sa.query("DELETE FROM s.Parent WHERE Code = N'counter'")

    assert after.id == before.id
    moved = {o.identity for o in after.objects if o.content_hash != before.get(o.identity).content_hash}
    assert extractor.raw_identity(f"db:{SCRATCH}", "sys.identity_columns") in moved
    assert extractor.raw_identity(f"db:{SCRATCH}", "sys.sequences") in moved
    assert f"{SCRATCH}.s.Parent" in moved          # its row count, informational
    assert {i for i in moved if not i.startswith("raw:")} == {f"{SCRATCH}.s.Parent"}


def test_an_unsupported_server_is_refused_before_anything_is_read():
    with pytest.raises(extractor.UnsupportedServer, match="before 2017"):
        extractor.check_version({"EngineEdition": "3", "Edition": "Enterprise Edition", "ProductVersion": "13.0.7000.253"})
    with pytest.raises(extractor.UnsupportedServer, match="only SQL Server itself"):
        extractor.check_version({"EngineEdition": "5", "Edition": "SQL Azure", "ProductVersion": "12.0.2000.8"})
    extractor.check_version({"EngineEdition": "3", "Edition": "Developer Edition", "ProductVersion": "14.0.3456.2"})
