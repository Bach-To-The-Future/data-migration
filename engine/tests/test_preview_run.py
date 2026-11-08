"""The tracer bullet, through the engine's library interface: one view from the sample
corpus converted end to end as a preview run (issue #36)."""
import ast
import json

VIEW = "RetailSample.sales.vOrderTotals"


def test_preview_run_generates_one_view_into_the_delivered_project_layout(engagement, extractor_server, tmp_path):
    from migrt import engine

    out = tmp_path / "out"
    result = engine.preview_run(engagement, extractor_server, artefacts=[VIEW], out_dir=out)

    produced = out / "converted" / "RetailSample" / "sales" / "views" / "vOrderTotals.py"
    assert produced.is_file()
    assert [a.file for a in result.artefacts] == [produced]
    tree = ast.parse(produced.read_text(encoding="utf-8"))
    assert [n.name for n in tree.body if isinstance(n, ast.FunctionDef)] == ["vOrderTotals"]


def test_generated_file_passes_layer_1(engagement, extractor_server, tmp_path):
    from migrt import engine

    result = engine.preview_run(engagement, extractor_server, artefacts=[VIEW], out_dir=tmp_path / "out")

    [artefact] = result.artefacts
    assert artefact.layer1.passed, artefact.layer1.failures
    assert {c.name for c in artefact.layer1.checks} >= {
        "parses", "lint", "import allow-list", "ANSI mode pinned", "source positions",
        "manual items accounted for",
    }


def test_report_data_says_not_reconciled_with_the_preview_banner(engagement, extractor_server, tmp_path):
    from migrt import engine

    result = engine.preview_run(engagement, extractor_server, artefacts=[VIEW], out_dir=tmp_path / "out")

    data = json.loads(result.report_data_path.read_text(encoding="utf-8"))
    assert result.report_data_path == tmp_path / "out" / "report" / "report-data.json"
    assert data["run"]["preview"] is True
    assert data["run"]["banner"].startswith("Preview run")
    assert data["run"]["snapshot"] == result.snapshot_id
    [item] = data["artefacts"]
    assert item["name"] == "RetailSample.sales.vOrderTotals"
    assert item["verdict"] == "Not reconciled"
    assert item["status"] == "Not reconciled"
    assert item["sign_off_group"] == "Not reconciled: preview run"
    assert "converted" not in {item["status"].lower()}
    assert {c["status"] for c in item["constructs"]} == {"Not reconciled (preview run)", "manual work"}


def test_unsupported_operations_are_reported_as_manual_work_never_dropped(engagement, extractor_server, tmp_path):
    """money arithmetic has no PySpark generation yet: the column stays in the file as a
    loud failure and is listed in the report, never silently left out."""
    from migrt import engine

    result = engine.preview_run(engagement, extractor_server, artefacts=[VIEW], out_dir=tmp_path / "out")

    data = json.loads(result.report_data_path.read_text(encoding="utf-8"))
    [item] = data["artefacts"]
    manual = [c for c in item["constructs"] if c["status"] == "manual work"]
    assert manual and all(c["reason"] and c["source_position"] for c in manual)
    assert "ComputedTotal" in result.artefacts[0].file.read_text(encoding="utf-8")


def test_report_data_is_deterministic_outside_its_run_specific_section(engagement, extractor_server, tmp_path):
    from migrt import engine

    first = engine.preview_run(engagement, extractor_server, artefacts=[VIEW], out_dir=tmp_path / "a")
    second = engine.preview_run(engagement, extractor_server, artefacts=[VIEW], out_dir=tmp_path / "b")

    def stable(path):
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("run_specific")
        return data

    assert stable(first.report_data_path) == stable(second.report_data_path)
    assert first.artefacts[0].file.read_bytes() == second.artefacts[0].file.read_bytes()


def test_a_view_whose_query_is_not_lowered_fails_when_called_never_returns_nothing(engagement, extractor_server, tmp_path):
    """TOP ... WITH TIES is not lowered yet: the query is kept as an opaque fragment, the
    file still passes layer 1, and calling the function raises instead of returning rows."""
    from migrt import engine

    view = "RetailSample.sales.vTopProducts"
    result = engine.preview_run(engagement, extractor_server, artefacts=[view], out_dir=tmp_path / "out")

    [artefact] = result.artefacts
    assert artefact.layer1.passed, artefact.layer1.failures
    assert [m.reason for m in artefact.manual_items] and "TOP" in artefact.manual_items[0].reason
    namespace = {}
    exec(compile(artefact.file.read_text(encoding="utf-8"), str(artefact.file), "exec"), namespace)

    class Session:
        class spark:
            class conf:
                @staticmethod
                def set(key, value):
                    pass

    import pytest
    with pytest.raises(RuntimeError, match="Manual work 1"):
        namespace["vTopProducts"](Session())


def test_report_data_carries_both_halves_of_the_differences_catalogue(engagement, extractor_server, tmp_path):
    """Caveats and manual-work reasons are worded from the catalogue, keyed by entry ID:
    SQL Server's behaviour from the source half, Spark's from the target half (issue #102)."""
    from migrt import engine
    from migrt.sources.sqlserver import differences as source
    from migrt.targets.pyspark import differences as target

    result = engine.preview_run(engagement, extractor_server, artefacts=[VIEW], out_dir=tmp_path / "out")

    data = json.loads(result.report_data_path.read_text(encoding="utf-8"))
    versions = data["run"]["versions"]
    assert versions["differences_source"] == source.half().version
    assert versions["differences_target"] == target.half().version
    [item] = data["artefacts"]
    sums = [c for c in item["constructs"] if c["operation"] in ("sum", "average") and c["status"] != "manual work"]
    assert sums and all(c["difference"] == "3.14" for c in sums)
    sql_server, spark = source.half().entry("3.14").wording, target.half().entry("3.14").wording
    assert sql_server and spark
    assert all(c["caveat"] == f"Silent difference 3.14: {sql_server}; {spark}" for c in sums)
    money_wording = source.half().entry("3.11").wording
    assert money_wording
    money = [c for c in item["constructs"] if c["status"] == "manual work" and "3.11" in c["reason"]]
    assert money and all(money_wording in c["reason"] for c in money)
