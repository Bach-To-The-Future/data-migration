"""Extraction from folders, alone or beside a live server, and the inventories of SSIS
packages and SQL Agent jobs (issue #40, spec section 3), through the engine's library
interface, on the sandbox."""
import json
import os
import pathlib

import pytest

from migrt import cli, engine
from migrt.core.catalog import CatalogReader
from migrt.sources.sqlserver import extractor, folders, names

from conftest import SCRATCH_BATCHES, sandbox_server

SAMPLE = pathlib.Path(__file__).parents[2] / "corpus" / "sample"
DB = engine.FolderSettings({"Db": "Latin1_General_CI_AS"}, "UTC", "2022", {"Db": 160})
SAMPLE_FOLDERS = [engine.Folder(SAMPLE / "database", "RetailSample"), engine.Folder(SAMPLE / "artefacts", "RetailSample")]
RETAIL = engine.FolderSettings(collations={"RetailSample": "SQL_Latin1_General_CP1_CI_AS"}, time_zone="UTC",
                               version="2022", compatibility_levels={"RetailSample": 160})
JOB = "agent_job:RetailSample - Nightly sales roll-up"
PACKAGE = "ssis_package:RetailSample/recognised/ssis/LoadDailySales.dtsx"


def facts(snapshot, identity):
    return {f.name: f.value for f in snapshot.get(identity).facts}


# What a file cannot say and a server can, each recorded as assumed in the folder snapshot:
# a constraint's generated name, a computed column's type, and an expression's normalised
# text; and what a server cannot say, such as an encrypted module's text and its defaults.
UNCOMPARED = {"secrets", "source_text", "references", "references_unavailable", "row_count", "ignored_for_identity",
              "assumed_values", "dependency_cross_check", "server_dependencies", "result_shape",
              "result_shape_unavailable", "unattributed_dependencies", "job_id", "not_interpreted",
              "not_extractable_reason", "with_encryption", "extractable", "parameters"}


def comparable(name, value):
    if name == "columns":
        return [{k: v for k, v in c.items() if k not in ("default", "computed_expression")
                 and not (c.get("computed") and k in ("declared_type", "system_type", "max_length", "precision",
                                                      "scale"))} | {"default_name": (c.get("default") or {}).get("name")}
                for c in value]
    if name == "check_constraints":
        return [{k: v for k, v in c.items() if k != "definition"} for c in value]
    if name == "unique_indexes":
        return [{k: v for k, v in c.items() if k != "filter"} for c in value]
    if name == "schedules":
        return [{k: v for k, v in s.items() if k != "active_start_date"} for s in value]
    return value


def unnamed(snapshot, identity):
    """The constraints whose names SQL Server generates, as the folder snapshot records them."""
    return {(a["fact"], a.get("where")) for a in facts(snapshot, identity).get("assumed_values", [])
            if a["property"] == "name"}


def differences(live, folder, *, unnamed_ok=False):
    out = []
    shared = {o.identity for o in live.objects if o.kind not in ("raw_capture", "server")} & {
        o.identity for o in folder.objects}
    for identity in sorted(shared):
        a, b = facts(live, identity), facts(folder, identity)
        if live.get(identity).kind != folder.get(identity).kind:
            out.append((identity, "kind"))
        for name in sorted((set(a) | set(b)) - UNCOMPARED):
            x, y = comparable(name, a.get(name)), comparable(name, b.get(name))
            if unnamed_ok and name in ("primary_key", "check_constraints") and unnamed(folder, identity):
                strip = (lambda v: None if v is None else {**v, "name": None}) if name == "primary_key" else (
                    lambda v: [{**c, "name": None} for c in v])
                x, y = strip(x), strip(y)
            if x != y:
                out.append((identity, name, x, y))
    return out


# ---------------------------------------------------------------- folders alone

def test_folders_alone_rebuild_the_sample_databases_catalog_as_the_server_has_it(engagement):
    folder = engine.extract(engagement, None, SAMPLE_FOLDERS, RETAIL)
    live = engine.extract(engagement, sandbox_server())

    assert differences(live, folder) == []
    live_objects = {o.identity for o in live.objects if o.kind not in ("raw_capture", "server")}
    assert live_objects - {o.identity for o in folder.objects} == set()
    # Every table, view, routine, trigger and type, and the job from its script, and
    # every parameter but the encrypted module's, whose text only the file holds.
    for o in folder.objects:
        if o.has_fact("parameters") and live.has(o.identity):
            assert o.fact("parameters").value == live.get(o.identity).fact("parameters").value, o.identity


def test_folders_alone_rebuild_a_case_sensitive_database_with_every_kind_of_object(engagement, scratch_database,
                                                                                    tmp_path):
    (tmp_path / "scratch.sql").write_text("\nGO\n".join(SCRATCH_BATCHES) + "\nGO\n", encoding="utf-8")
    folder = engine.extract(engagement, None, [engine.Folder(tmp_path, scratch_database)],
                            engine.FolderSettings({scratch_database: "Latin1_General_CS_AS"}, "UTC", "2022",
                                                  {scratch_database: 160}))
    live = engine.extract(engagement, sandbox_server(databases=(scratch_database,)))

    assert differences(live, folder, unnamed_ok=True) == []
    # A file holds an encrypted module's text, which the server hides.
    hidden = facts(folder, "MigrtExtractTest.s.uspHidden")
    assert hidden["extractable"] is True and hidden["with_encryption"] is True
    assert hidden["parameters"][0]["default"] == "1"
    # Security statements build nothing, and are listed.
    skipped = facts(folder, folders.FOLDERS_IDENTITY)["not_replayed"]
    assert {s["statement"] for s in skipped} == {"CreateUserStatement", "GrantStatement"}


def test_create_statements_run_before_alter_statements_in_file_name_order(engagement, tmp_path):
    (tmp_path / "a_alter.sql").write_text("ALTER TABLE dbo.T ADD B varchar(5) NULL;\n"
                                          "ALTER TABLE dbo.T ADD CONSTRAINT PK_T PRIMARY KEY (A);\n", encoding="utf-8")
    (tmp_path / "b_create.sql").write_text("CREATE TABLE dbo.T (A int NOT NULL);\n", encoding="utf-8")
    (tmp_path / "c_alter.sql").write_text("ALTER TABLE dbo.T ALTER COLUMN B varchar(9) NOT NULL;\n", encoding="utf-8")

    snapshot = engine.extract(engagement, None, [engine.Folder(tmp_path, "Db")],
                              DB)

    t = facts(snapshot, "Db.dbo.T")
    assert [(c["name"], c["max_length"], c["nullable"]) for c in t["columns"]] == [("A", 4, False), ("B", 9, False)]
    assert t["primary_key"]["name"] == "PK_T"
    detail = snapshot.get("Db.dbo.T").fact("columns").provenance
    assert detail.kind == "file" and detail.detail == "Db/b_create.sql line 1; Db/a_alter.sql line 1; " \
                                                      "Db/a_alter.sql line 2; Db/c_alter.sql line 1"


def test_a_second_create_of_an_object_is_an_error_naming_both_files(engagement, tmp_path):
    (tmp_path / "one.sql").write_text("CREATE TABLE dbo.T (A int);\n", encoding="utf-8")
    (tmp_path / "two.sql").write_text("-- again\nCREATE TABLE dbo.t (A int);\n", encoding="utf-8")

    with pytest.raises(engine.DuplicateCreate) as raised:
        engine.extract(engagement, None, [engine.Folder(tmp_path, "Db")],
                       DB)

    assert "Db/one.sql line 1" in str(raised.value) and "Db/two.sql line 2" in str(raised.value)




@pytest.mark.parametrize("files, named", [
    ({"a.sql": "CREATE OR ALTER VIEW dbo.V AS SELECT 1 AS a;", "b.sql": "CREATE VIEW dbo.V AS SELECT 2 AS a;"},
     ("Db/a.sql line 1", "Db/b.sql line 1")),
    ({"a.sql": "EXEC msdb.dbo.sp_add_job @job_name = N'J';", "b.sql": "EXEC msdb.dbo.sp_add_job @job_name = N'J';"},
     ("Db/a.sql line 1", "Db/b.sql")),
])
def test_a_create_or_alter_or_a_job_made_twice_is_a_duplicate_too(engagement, tmp_path, files, named):
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")

    with pytest.raises(engine.DuplicateCreate) as raised:
        engine.extract(engagement, None, [engine.Folder(tmp_path, "Db")], DB)

    assert all(n in str(raised.value) for n in named)


def test_create_index_runs_after_the_other_creates_so_an_index_file_may_sort_first(engagement, tmp_path):
    """Script exports put indexes in files of their own, which can sort before their tables'
    (spec section 3: CREATE, then CREATE INDEX, then ALTER, each in file-name order)."""
    (tmp_path / "a_index.sql").write_text("CREATE UNIQUE INDEX UX ON dbo.T (A);", encoding="utf-8")
    (tmp_path / "b_table.sql").write_text("CREATE TABLE dbo.T (A int NOT NULL);", encoding="utf-8")
    (tmp_path / "c_index.sql").write_text("CREATE UNIQUE INDEX UX2 ON dbo.T (A DESC);", encoding="utf-8")

    snapshot = engine.extract(engagement, None, [engine.Folder(tmp_path, "Db")], DB)

    assert [i["name"] for i in facts(snapshot, "Db.dbo.T")["unique_indexes"]] == ["UX", "UX2"]


def test_a_duplicate_create_index_is_an_error_too(engagement, tmp_path):
    (tmp_path / "a.sql").write_text("CREATE TABLE dbo.T (A int NOT NULL);\nCREATE UNIQUE INDEX UX ON dbo.T (A);",
                                    encoding="utf-8")
    (tmp_path / "b.sql").write_text("CREATE UNIQUE INDEX UX ON dbo.T (A);", encoding="utf-8")

    with pytest.raises(engine.DuplicateCreate, match="Db/a.sql line 2.*Db/b.sql line 1"):
        engine.extract(engagement, None, [engine.Folder(tmp_path, "Db")], DB)


def test_a_sequences_defaults_are_assumed_and_nothing_is_dropped_unrecorded(engagement, tmp_path):
    (tmp_path / "a.sql").write_text("CREATE TABLE dbo.T (A int NOT NULL);\nCREATE INDEX IX ON dbo.T (A);\n"
                                    "CREATE SEQUENCE dbo.S AS decimal(4, 0) START WITH 5;", encoding="utf-8")

    snapshot = engine.extract(engagement, None, [engine.Folder(tmp_path, "Db")], DB)

    sequence = facts(snapshot, "Db.dbo.S")
    assert (sequence["sequence"]["minimum"], sequence["sequence"]["maximum"]) == (-9999, 9999)
    assert {a["property"] for a in sequence["assumed_values"]} == {"increment", "minimum", "maximum", "cycles",
                                                                   "cached", "cache_size"}
    [skipped] = facts(snapshot, folders.FOLDERS_IDENTITY)["not_replayed"]
    assert (skipped["line"], skipped["reason"]) == (2, folders.NOT_UNIQUE)


def test_folder_settings_beside_a_server_are_refused_not_ignored(engagement):
    with pytest.raises(engine.FolderExtractionError, match="only without a server"):
        engine.extract(engagement, sandbox_server(), SAMPLE_FOLDERS, RETAIL)


def test_the_collation_time_zone_version_and_level_are_required_without_a_server(engagement):
    with pytest.raises(engine.SettingsRequired) as raised:
        engine.extract(engagement, None, SAMPLE_FOLDERS, engine.FolderSettings())

    assert raised.value.missing == ["the time zone, which a server would have supplied",
                                    "the SQL Server version, which selects the sandbox",
                                    "the default collation of database RetailSample",
                                    "the compatibility level of database RetailSample, which selects the parser"]


def test_everything_else_a_server_would_have_supplied_is_recorded_as_assumed(engagement):
    snapshot = engine.extract(engagement, None, SAMPLE_FOLDERS, RETAIL)

    server = snapshot.get(folders.NO_SERVER)
    assert {f.provenance.kind for f in server.facts} == {"assumed"}
    database = snapshot.get("RetailSample")
    assert {f.provenance.kind for f in database.facts} == {"assumed"}
    level = database.fact("compatibility_level", extractor.SOURCE)
    assert (level.value, level.provenance.detail) == (160, "the compatibility level setting")
    assert server.fact("version").provenance.detail == "the version setting: SQL Server 2022"
    view = snapshot.get("RetailSample.sales.vOrderTotals")
    assert view.fact("ansi_nulls", extractor.SOURCE).provenance.kind == "assumed"
    assert view.fact("source_text").provenance == view.fact("name_parts").provenance
    assert view.fact("source_text").provenance.kind == "file"
    # Values inside parsed facts that a server would have given: a column's collation and
    # nullability, and a trigger's firing order.
    customer = {(a["column"], a["property"]): a for a in facts(snapshot, "RetailSample.ref.Customer")["assumed_values"]
                if a["fact"] == "columns"}
    assert customer[("Name", "collation")]["value"] == "SQL_Latin1_General_CP1_CI_AS"
    product = facts(snapshot, "RetailSample.ref.Product")["assumed_values"]
    assert not any(a.get("column") == "Sku" for a in product)   # its COLLATE clause is in the file
    trigger = facts(snapshot, "RetailSample.sales.trOrder_AuditStatus")["assumed_values"]
    assert [a["fact"] for a in trigger] == ["firing_order"]


def test_the_report_has_a_completeness_summary_and_flags_every_assumed_collation(engagement, tmp_path):
    result = engine.preview_run(engagement, None, ["RetailSample.sales.vOrderTotals"], tmp_path, SAMPLE_FOLDERS,
                                RETAIL)
    snapshot = engine.read_snapshot(engagement, result.snapshot_id)

    [artefact] = result.artefacts
    assert artefact.layer1.passed, artefact.layer1.failures
    data = json.loads(result.report_data_path.read_text(encoding="utf-8"))
    extraction = data["extraction"]
    counts = extraction["completeness"]["facts"]
    assert counts["read"] == 0 and counts["parsed"] > 0 and counts["assumed"] > 0
    assert extraction["completeness"]["assumed_values"] > 0
    collations = {(c["object"], c["column"]) for c in extraction["assumed_collations"]}
    expected = {(o.identity, c["name"]) for o in snapshot.objects if o.kind in ("table", "table_type")
                for c in o.fact("columns").value if c["collation"] is not None
                and not (o.identity == "RetailSample.ref.Product" and c["name"] == "Sku")}
    expected |= {(o.identity, c["name"]) for o in snapshot.objects if o.kind == "table_function"
                 for c in o.fact("result_shape").value if c["system_type"] in folders.COLLATABLE}
    assert collations == expected and ("RetailSample.ref.Customer", "Name") in collations
    assert data["run"]["source"]["assumed"] == ["build", "collation", "edition", "engine_edition", "nested_triggers",
                                                "time_zone", "version"]
    assert extraction["folders"]["lost_without_a_server"] == list(folders.LOST_WITHOUT_A_SERVER)
    kinds = {d["kind"] for d in data["degradations"]}
    assert {"assumption", "reduced coverage", "skipped check"} <= kinds


def test_a_query_batch_from_a_folder_is_an_artefact_with_its_references(engagement):
    snapshot = engine.extract(engagement, None, SAMPLE_FOLDERS, RETAIL)

    batch = facts(snapshot, "batch:RetailSample/batches/load_price_staging.sql")
    assert snapshot.get("batch:RetailSample/batches/load_price_staging.sql").kind == "query_batch"
    resolved = {r["resolved"] for r in batch["references"]}
    assert {"RetailSample.stg.ProductPriceImport", "RetailSample.ref.Product", "RetailSample.sales.OrderLine",
            "RetailSample.sales.Order", "RetailSample.sales.uspApplyPriceImport"} <= resolved
    roles = {f["file"]: f["role"] for f in facts(snapshot, folders.FOLDERS_IDENTITY)["files"]}
    assert roles["RetailSample/batches/load_price_staging.sql"] == folders.QUERY_BATCH
    assert roles["RetailSample/01_schema.sql"] == folders.DATA_DEFINITION
    assert roles["RetailSample/recognised/agent_jobs/nightly_sales_rollup.sql"] == folders.JOB_SCRIPT
    assert roles["RetailSample/recognised/ssis/LoadDailySales.dtsx"] == folders.PACKAGE


def test_every_file_is_kept_verbatim_and_every_other_file_is_listed(engagement, tmp_path):
    text = "CREATE TABLE dbo.T (A int)\r\nGO\r\n"
    (tmp_path / "t.sql").write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    (tmp_path / "notes.txt").write_text("not read", encoding="utf-8")

    snapshot = engine.extract(engagement, None, [engine.Folder(tmp_path, "Db")],
                              DB)

    raw = facts(snapshot, "raw:file:Db/t.sql")
    assert raw["text"] == text and raw["encoding"] == "utf-16"
    files = facts(snapshot, folders.FOLDERS_IDENTITY)["files"]
    assert {"file": "Db/notes.txt", "role": folders.NOT_READ, "reason": "not a .sql or .dtsx file"} in files


def test_one_command_previews_a_view_from_folders_alone(engagement, tmp_path, capsys):
    common = ["preview", "--engagement", engagement.name, "--control-dsn", os.environ["MIGRT_CONTROL_DSN"],
              "--no-server", "--folder", f"RetailSample={SAMPLE / 'database'}",
              "--folder", f"RetailSample={SAMPLE / 'artefacts'}", "--time-zone", "UTC",
              "--artefact", "RetailSample.sales.vOrderTotals", "--out", str(tmp_path)]

    assert cli.main(common) == 3
    assert "the default collation of database RetailSample" in capsys.readouterr().err
    assert cli.main(common + ["--collation", "RetailSample=SQL_Latin1_General_CP1_CI_AS",
                              "--sql-server-version", "2022", "--compatibility-level", "RetailSample=160"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert [a["status"] for a in summary["artefacts"]] == ["Not reconciled"]
    assert (tmp_path / "converted/RetailSample/sales/views/vOrderTotals.py").is_file()


# ---------------------------------------------------------------- beside a live server

def test_beside_a_live_server_its_objects_come_from_the_server_and_files_add_batches_and_packages(engagement):
    snapshot = engine.extract(engagement, sandbox_server(), SAMPLE_FOLDERS)

    order = snapshot.get("RetailSample.sales.Order")
    assert {f.provenance.kind for f in order.facts} == {"server"}
    assert snapshot.has("batch:RetailSample/batches/load_price_staging.sql") and snapshot.has(PACKAGE)
    folder = facts(snapshot, folders.FOLDERS_IDENTITY)
    superseded = {s["file"] for s in folder["superseded"]}
    assert "RetailSample/01_schema.sql" in superseded and "RetailSample/views/sales.vOrderTotals.sql" in superseded
    assert "RetailSample/recognised/agent_jobs/nightly_sales_rollup.sql" in superseded
    assert facts(snapshot, JOB)["job_id"] is not None   # from msdb
    assert folder["lost_without_a_server"] == []


def test_a_folder_for_a_database_the_server_extraction_does_not_read_is_refused(engagement, tmp_path):
    with pytest.raises(engine.FolderExtractionError, match="Elsewhere"):
        engine.extract(engagement, sandbox_server(), [engine.Folder(tmp_path, "Elsewhere")])


# ---------------------------------------------------------------- recognised artefacts

def test_an_ssis_package_is_inventoried_with_its_references_to_catalog_objects(engagement):
    snapshot = engine.extract(engagement, sandbox_server(), SAMPLE_FOLDERS)

    package = facts(snapshot, PACKAGE)
    assert package["inventoried"] and [t["name"] for t in package["tasks"]] == [
        "Truncate staging", "Load price file", "Apply prices"]
    resolved = {(r["task"], r.get("component"), r["resolved"]) for r in package["references"]}
    assert resolved == {("Package\\Truncate staging", None, "RetailSample.stg.ProductPriceImport"),
                        ("Package\\Apply prices", None, "RetailSample.sales.uspApplyPriceImport"),
                        ("Package\\Load price file", "Staging destination", "RetailSample.stg.ProductPriceImport")}
    assert package["references_unavailable"] == []
    # The package is what calls the procedure: an entry point.
    reader = CatalogReader(snapshot, names.resolve)
    assert PACKAGE in reader.referrers("RetailSample.sales.uspApplyPriceImport")


def test_a_jobs_steps_are_parsed_for_references_and_show_its_entry_points(engagement):
    snapshot = engine.extract(engagement, sandbox_server())

    job = facts(snapshot, JOB)
    assert {(r["step_id"], r["resolved"]) for r in job["references"]} == {
        (1, "RetailSample.sales.uspRecalculateDailySales"), (2, "RetailSample.sales.uspCloseStaleOrders")}
    assert job["references_unavailable"] == []
    reader = CatalogReader(snapshot, names.resolve)
    assert reader.referrers("RetailSample.sales.uspCloseStaleOrders") == [JOB]


def test_a_job_script_in_a_folder_is_inventoried_as_msdb_holds_the_job(engagement):
    folder = engine.extract(engagement, None, SAMPLE_FOLDERS, RETAIL)
    live = engine.extract(engagement, sandbox_server())

    scripted, read = facts(folder, JOB), facts(live, JOB)
    for name in ("enabled", "description", "category", "start_step", "steps", "references"):
        assert scripted[name] == read[name], name
    assert comparable("schedules", scripted["schedules"]) == comparable("schedules", read["schedules"])
    assumed = {(a["fact"], a["property"]) for a in scripted["assumed_values"]}
    assert ("schedules", "active_start_date") in assumed and ("schedules", "freq_subday_type") in assumed
    assert folder.get(JOB).fact("job_id").provenance.kind == "assumed"


def test_packages_and_jobs_are_listed_as_manual_work_with_secrets_masked(engagement, tmp_path):
    packages = tmp_path / "packages"
    packages.mkdir()
    source = (SAMPLE / "artefacts" / "recognised" / "ssis" / "LoadDailySales.dtsx").read_text(encoding="utf-8")
    (packages / "WithPassword.dtsx").write_text(source.replace("Integrated Security=SSPI;",
                                                               "User ID=loader;Password=Hunter2!;"), encoding="utf-8")
    (packages / "Locked.dtsx").write_text('<?xml version="1.0"?>\n<DTS:Executable xmlns:DTS="www.microsoft.com/'
                                          'SqlServer/Dts" DTS:ObjectName="Locked" DTS:ProtectionLevel="4">'
                                          '<EncryptedData /></DTS:Executable>', encoding="utf-8")

    result = engine.preview_run(engagement, sandbox_server(), ["RetailSample.sales.vOrderTotals"], tmp_path / "out",
                                [engine.Folder(packages, "RetailSample")])

    text = result.report_data_path.read_text(encoding="utf-8")
    assert "Hunter2!" not in text and "loader" not in text
    data = json.loads(text)
    recognised = {r["name"]: r for r in data["recognised_artefacts"]}
    assert set(recognised) == {JOB, "ssis_package:RetailSample/WithPassword.dtsx", "ssis_package:RetailSample/Locked.dtsx"}
    assert all((r["status"], r["sign_off_group"]) == ("Recognised, not converted", "Manual work")
               for r in recognised.values())
    assert sorted(data["sign_off"]["groups"]["Manual work"]) == sorted(recognised)
    package = recognised["ssis_package:RetailSample/WithPassword.dtsx"]
    [connection] = [c for c in package["inventory"]["connections"] if c["name"] == "RetailSample"]
    setting = "ssis_package:RetailSample/WithPassword.dtsx.secret_1"
    assert connection["connection_string"] == f"[secret: setting {setting}]"
    assert setting in [s["setting"] for s in package["secrets"]]
    assert package["entry_points"] == ["RetailSample.sales.uspApplyPriceImport", "RetailSample.stg.ProductPriceImport"]
    locked = recognised["ssis_package:RetailSample/Locked.dtsx"]
    assert locked["inventory"]["inventoried"] is False and "encrypt all" in locked["inventory"]["not_inventoried_reason"]
    assert recognised[JOB]["entry_points"] == ["RetailSample.sales.uspCloseStaleOrders",
                                               "RetailSample.sales.uspRecalculateDailySales"]
