"""The delivered project (issue #44, spec section 6, "The delivered project"): the layout with its name adjustments,
the manifest of entry points, the settings file generated from the catalog snapshot and the engagement, the
representation rules of the type mapping, and source comments marked as from the original. A project imports
as it ships: a callee is imported as a module, so a dependency is an import."""
import importlib
import inspect
import json
import pathlib
import sys

from conftest import sandbox_server

ROOT = pathlib.Path(__file__).resolve().parents[2]
VIEW = "RetailSample.sales.vOrderTotals"
LABEL_VIEW = "RetailSample.ref.vActiveCustomers"
LABEL_FUNCTION = "RetailSample.ref.fnFormatCustomerLabel"
RATIO = "RetailSample.ref.fnSafeRatio"
INLINE = "RetailSample.sales.fnOrderLines"
PROCEDURE = "RetailSample.sales.uspCancelOrder"
TRIGGER = "RetailSample.sales.trOrder_AuditStatus"
BATCH = "batch:RetailSample/batches/customer_dedup.sql"
ALL = [VIEW, LABEL_VIEW, LABEL_FUNCTION, RATIO, INLINE, PROCEDURE, TRIGGER]


def run(engagement, server, out, artefacts=ALL, **kw):
    from migrt import engine

    return engine.preview_run(engagement, server, artefacts=artefacts, out_dir=out, **kw)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_project_follows_the_decided_layout_with_a_package_for_every_folder(engagement, extractor_server, tmp_path):
    run(engagement, extractor_server, tmp_path)

    base = tmp_path / "converted" / "RetailSample"
    for produced in (base / "sales" / "views" / "vOrderTotals.py", base / "ref" / "views" / "vActiveCustomers.py",
                     base / "ref" / "functions" / "fnSafeRatio.py", base / "sales" / "functions" / "fnOrderLines.py",
                     base / "sales" / "procedures" / "uspCancelOrder.py",
                     base / "sales" / "triggers" / "trOrder_AuditStatus.py"):
        assert produced.is_file(), produced
    folders = {p.parent for p in (tmp_path / "converted").rglob("*.py") if p.name != "__init__.py"}
    folders |= {p for f in folders for p in f.parents if p != tmp_path and tmp_path in p.parents}
    assert all((folder / "__init__.py").is_file() for folder in folders), sorted(map(str, folders))
    for name in ("manifest.json", "settings.json", "representation-rules.md"):
        assert (tmp_path / name).is_file(), name


def test_a_query_batch_read_from_a_folder_is_generated_under_its_databases_batches_folder(engagement, tmp_path):
    from migrt import engine

    settings = engine.FolderSettings(collations={"RetailSample": "SQL_Latin1_General_CP1_CI_AS"}, time_zone="UTC",
                                     version="2022", compatibility_levels={"RetailSample": 160})
    folders = [engine.Folder(ROOT / "corpus" / "sample" / "database", "RetailSample"),
               engine.Folder(ROOT / "corpus" / "sample" / "artefacts", "RetailSample")]
    result = engine.preview_run(engagement, None, artefacts=[BATCH], out_dir=tmp_path, folders=folders, settings=settings)

    produced = tmp_path / "converted" / "RetailSample" / "batches" / "batches_customer_dedup.py"
    assert [a.file for a in result.artefacts] == [produced]
    assert result.artefacts[0].layer1.passed, result.artefacts[0].layer1.failures
    text = produced.read_text(encoding="utf-8")
    assert "BATCHES = [batch_1" in text
    recorded = engine.name_adjustments(engagement)
    assert recorded[("python-module", "RetailSample.batches")]["batches/customer_dedup"] == "batches_customer_dedup"


def test_every_module_of_the_project_imports_and_defines_what_the_manifest_says(engagement, extractor_server, tmp_path):
    run(engagement, extractor_server, tmp_path)
    manifest = read_json(tmp_path / "manifest.json")

    sys.path.insert(0, str(tmp_path))
    try:
        for entry in manifest["entry_points"]:
            module = importlib.import_module(entry["module"])
            for function in entry["functions"]:
                assert callable(getattr(module, function)), (entry["module"], function)
            [first, *_] = entry["functions"]
            parameters = list(inspect.signature(getattr(module, first)).parameters)
            assert parameters == [a for a in entry["signature"].split(", ") if a != "*"], entry["artefact"]
            assert [p["python_name"] for p in entry["parameters"]] == [
                a for a in parameters[1:] if a not in ("inserted", "deleted")][:len(entry["parameters"])]
    finally:
        sys.path.remove(str(tmp_path))
        for name in [m for m in sys.modules if m == "converted" or m.startswith("converted.")]:
            del sys.modules[name]


def test_the_manifest_has_one_entry_point_per_artefact_saying_how_to_invoke_it_and_read_its_results(
        engagement, extractor_server, tmp_path):
    run(engagement, extractor_server, tmp_path)

    manifest = read_json(tmp_path / "manifest.json")
    entries = {e["artefact"]: e for e in manifest["entry_points"]}
    assert sorted(entries) == sorted(ALL) and manifest["snapshot"]
    view = entries[VIEW]
    assert view["type"] == "view" and view["status"] == "generated with manual work, not reconciled"   # the money column
    assert view["module"] == "converted.RetailSample.sales.views.vOrderTotals" and view["functions"] == ["vOrderTotals"]
    assert view["call"] == "vOrderTotals(ss)" and "DataFrame" in view["result"]
    assert [c["name"] for c in view["columns"]][:3] == ["OrderId", "CustomerId", "OrderDate"]
    assert view["columns"][0]["spark_type"] == "int" and view["columns"][1]["spark_type"] == "int"
    function = entries[INLINE]
    assert function["call"] == "fnOrderLines(ss, OrderId)"
    assert function["parameters"] == [{"name": "@OrderId", "python_name": "OrderId", "direction": "input",
                                       "readonly": False, "default": None,
                                       "type": {"name": "integer", "bits": 32, "signed": True}, "spark_type": "int"}]
    assert entries[RATIO]["result"].startswith("a Column")
    procedure = entries[PROCEDURE]
    assert procedure["status"] == "manual work" and procedure["manual_items"] > 0
    assert "raises" in procedure["result"] and procedure["call"].startswith("uspCancelOrder(ss, *, ")
    assert entries[TRIGGER]["call"] == "trOrder_AuditStatus(ss, inserted, deleted)"


def test_the_settings_file_holds_the_catalogs_semantic_settings_with_their_origin_and_a_fingerprint(
        engagement, extractor_server, tmp_path):
    run(engagement, extractor_server, tmp_path)

    settings = read_json(tmp_path / "settings.json")
    semantic = settings["semantic"]
    assert settings["snapshot"] and settings["engagement"] == engagement.name
    assert semantic["databases"]["RetailSample"]["default_collation"] == {
        "value": "SQL_Latin1_General_CP1_CI_AS", "origin": "read from the server"}
    assert semantic["databases"]["RetailSample"]["compatibility_level"]["value"] >= 130
    assert semantic["time_zone"]["origin"].startswith(("read from the server", "assumed"))
    assert semantic["transaction_mechanism"] == {"value": "compensating restore", "origin": "default (ADR 0002)"}
    assert semantic["identity_mechanism"]["value"] == "write wrapper counters"
    for name in ("datefirst", "dateformat", "language"):
        assert semantic[name]["origin"].startswith("assumed")
    assert settings["fingerprint"] and len(settings["fingerprint"]) == 64
    assert {"SUSER_SNAME", "HOST_NAME", "DB_NAME", "SPID"} <= set(settings["runtime_identity"])
    assert all(v["override"] is None for v in settings["runtime_identity"].values())


def test_the_settings_a_secret_stands_for_are_listed_to_be_filled_in_and_never_hold_the_secret(
        engagement, scratch_database, tmp_path):
    from migrt import engine

    server = sandbox_server(databases=(scratch_database,))
    identity = f"{scratch_database}.s.vSecret"
    result = engine.preview_run(engagement, server, artefacts=[identity], out_dir=tmp_path)

    settings = read_json(tmp_path / "settings.json")
    report = read_json(result.report_data_path)
    [item] = report["artefacts"]
    names = sorted(s["setting"] for s in item["secrets"])
    assert names == [f"{identity}.secret_1", f"{identity}.secret_2"]
    assert settings["secrets"] == {n: {"value": None, "required_by": [identity], "read_by_code": False} for n in names}
    assert "Hunter2" not in (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert all("nothing reads settings yet" not in s["text"] and "settings file lists it" in s["text"]
               for s in item["secrets"])


def test_the_representation_rules_are_published_in_the_project(engagement, extractor_server, tmp_path):
    from migrt.targets.pyspark import types

    run(engagement, extractor_server, tmp_path)

    assert (tmp_path / "representation-rules.md").read_text(encoding="utf-8") == types.representation_rules_markdown()


def test_source_comments_appear_beside_the_generated_code_marked_as_from_the_original(
        engagement, extractor_server, tmp_path):
    run(engagement, extractor_server, tmp_path)

    text = (tmp_path / "converted/RetailSample/sales/views/vOrderTotals.py").read_text(encoding="utf-8")
    lines = text.splitlines()
    at = next(i for i, line in enumerate(lines) if "# original: -- AVG over an int column returns int" in line)
    assert 'alias("AvgUnitsPerLine")' in lines[at + 1] or "AvgUnitsPerLine" in " ".join(lines[at:at + 3])
    procedure = (tmp_path / "converted/RetailSample/sales/procedures/uspCancelOrder.py").read_text(encoding="utf-8")
    assert "# original: --" in procedure or "# original: /*" in procedure


def test_a_dependency_between_generated_artefacts_is_an_import_and_otherwise_manual_work(engagement, tmp_path):
    from migrt import engine

    files = tmp_path / "files"
    files.mkdir()
    (files / "01_schema.sql").write_text(
        (ROOT / "corpus" / "sample" / "database" / "01_schema.sql").read_text(encoding="utf-8"), encoding="utf-8")
    (files / "02_function.sql").write_text(
        "CREATE FUNCTION ref.fnSafeRatio (@Numerator int, @Denominator int) RETURNS int AS BEGIN\n"
        "RETURN CASE WHEN @Denominator = 0 THEN NULL ELSE @Numerator / @Denominator END;\nEND;\nGO\n", encoding="utf-8")
    (files / "03_view.sql").write_text(
        "CREATE VIEW sales.vRatios AS SELECT o.OrderId, ref.fnSafeRatio(o.Status, o.CustomerId) AS Ratio\n"
        "FROM sales.[Order] AS o;\nGO\n", encoding="utf-8")
    settings = engine.FolderSettings(collations={"RetailSample": "SQL_Latin1_General_CP1_CI_AS"}, time_zone="UTC",
                                     version="2022", compatibility_levels={"RetailSample": 160})
    folders = [engine.Folder(files, "RetailSample")]
    view, function = "RetailSample.sales.vRatios", "RetailSample.ref.fnSafeRatio"
    together = engine.preview_run(engagement, None, artefacts=[view, function], out_dir=tmp_path / "together",
                                 folders=folders, settings=settings)
    alone = engine.preview_run(engagement, None, artefacts=[view], out_dir=tmp_path / "alone",
                          folders=folders, settings=settings)

    text = together.artefacts[0].file.read_text(encoding="utf-8")
    assert "from converted.RetailSample.ref.functions.fnSafeRatio import fnSafeRatio" in text
    assert "fnSafeRatio(ss, " in text
    assert together.artefacts[0].layer1.passed, together.artefacts[0].layer1.failures
    assert together.artefacts[0].manual_items == []
    assert "import fnSafeRatio" not in alone.artefacts[0].file.read_text(encoding="utf-8")
    assert any("is a function that is not generated in this project" in m.reason for m in alone.artefacts[0].manual_items)
    assert alone.artefacts[0].layer1.passed, alone.artefacts[0].layer1.failures


def test_every_corpus_procedure_and_trigger_generates_passes_layer_1_and_is_reported(engagement, extractor_server, tmp_path):
    from migrt import engine

    snapshot = engine.extract(engagement, extractor_server)
    identities = sorted(o.identity for o in snapshot.objects
                        if o.kind in ("procedure", "trigger", "view", "scalar_function", "inline_function", "table_function")
                        and o.identity.startswith("RetailSample.") and o.fact("extractable").value)
    result = run(engagement, extractor_server, tmp_path, artefacts=identities)

    assert len(result.artefacts) == len(identities) >= 25
    for artefact in result.artefacts:
        assert artefact.layer1.passed, (artefact.identity, artefact.layer1.failures)
    report = read_json(result.report_data_path)
    constructs = [c for item in report["artefacts"] for c in item["constructs"]]
    assert any("scope_may_be_stale" in c and c["status"] == "manual work" and c["reason"] for c in constructs)
    assert not [c for item in report["artefacts"] if item["type"] in ("procedure", "trigger")
                for c in item["constructs"] if c["status"] != "manual work" and c["operation"] != "declare_parameter"]
    assert report["project"]["entry_points"] == len(identities) and report["project"]["manifest"] == "manifest.json"
    assert "converted" not in json.dumps(report["sign_off"]).lower()


def test_a_construct_replaced_by_a_failure_is_reported_as_manual_work_with_the_reason_of_the_one_that_failed(
        engagement, extractor_server, tmp_path):
    result = run(engagement, extractor_server, tmp_path, artefacts=[LABEL_VIEW])

    [item] = read_json(result.report_data_path)["artefacts"]
    assert all(c["status"] == "manual work" for c in item["constructs"]), item["constructs"]
    assert any("in_list has no PySpark generation" in c["reason"] for c in item["constructs"])
    assert any("its code is not generated, because of manual work 1" in c["reason"] for c in item["constructs"])


def test_the_whole_project_is_deterministic_outside_the_reports_run_specific_section(engagement, extractor_server, tmp_path):
    run(engagement, extractor_server, tmp_path / "a")
    run(engagement, extractor_server, tmp_path / "b")

    def tree(root):
        return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
                and p.name != "report-data.json"}

    assert tree(tmp_path / "a") == tree(tmp_path / "b")
    assert len(tree(tmp_path / "a")) > 10


def test_generated_assumptions_reach_the_report(engagement, extractor_server, tmp_path):
    result = run(engagement, extractor_server, tmp_path)

    report = read_json(result.report_data_path)
    text = json.dumps(report["degradations"])
    assert "DATEFIRST" in text and "DATEFORMAT" in text and "not read from the server" in text
