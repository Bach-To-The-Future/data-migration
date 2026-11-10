"""Procedure result shapes, described in the sandbox (issue #41, spec section 3), through the
engine's library interface, on the sandbox: its own scratch database for what the corpus lacks,
and the three corpus databases for the headline number.

The sandbox is the tool's own SQL Server with full rights. In these tests it is the same
server as the "client's", which the extractor reads only through its read-only login."""
import csv
import dataclasses
import io
import json
import os
import pathlib
import re

import pytest

from migrt import cli, engine
from migrt.core.catalog import CatalogReader, Provenance
from migrt.sources.sqlserver import folders, names, shapes

from conftest import sa_connection, sandbox_server

SHAPE_DB = "MigrtShapeTest"
SAMPLE = pathlib.Path(__file__).parents[2] / "corpus" / "sample"
ANNEX_F = pathlib.Path(__file__).parents[2] / "docs" / "spec" / "annex-f-extractor-scripts.md"

# A case-sensitive database with what the corpus lacks for shapes: objects the sandbox must
# recreate in dependency order (a view over a view whose name sorts first, a function used in
# a procedure's select list, a sequence, a table type, a synonym, an alias type), settings that
# change what code means (QUOTED_IDENTIFIER and ANSI_NULLS OFF), and procedures SQL Server
# cannot describe (a temp table, dynamic SQL, encryption, a name in the wrong case).
SHAPE_BATCHES = [
    "CREATE SCHEMA s",
    "CREATE TYPE dbo.Code FROM nvarchar(10) NOT NULL",
    "CREATE TYPE s.Lines AS TABLE (Id int PRIMARY KEY, Qty int NOT NULL)",
    "CREATE SEQUENCE s.Seq AS bigint START WITH 100 INCREMENT BY 5",
    """CREATE TABLE s.Parent (
        Id int IDENTITY(10, 2) CONSTRAINT PK_Parent PRIMARY KEY,
        Code dbo.Code NOT NULL,
        Note varchar(20) COLLATE Latin1_General_CI_AS NULL,
        Price decimal(10, 2) NOT NULL,
        Twice AS (Id * 2) PERSISTED)""",
    "CREATE TABLE s.Child (Id int NOT NULL, ParentId int NOT NULL REFERENCES s.Parent (Id), Email varchar(50) NULL)",
    "CREATE SYNONYM dbo.Parents FOR s.Parent",
    "CREATE VIEW s.vParents AS SELECT p.Id, p.Twice FROM dbo.Parents AS p",
    "CREATE VIEW s.vNested AS SELECT v.Id FROM s.vParents AS v",
    "CREATE FUNCTION s.fnDouble (@x int) RETURNS int AS BEGIN RETURN @x * 2 END",
    "CREATE FUNCTION s.fnChildren (@parent int) RETURNS TABLE AS RETURN SELECT c.Id FROM s.Child AS c "
    "WHERE c.ParentId = @parent",
    "CREATE PROCEDURE s.uspPlain AS SELECT p.Id, p.Code, p.Note, p.Price FROM dbo.Parents AS p",
    "CREATE PROCEDURE s.uspCaller AS SELECT s.fnDouble(1) AS d, f.Id FROM s.fnChildren(1) AS f",
    "CREATE PROCEDURE s.uspViews AS SELECT v.Id FROM s.vNested AS v",
    "CREATE PROCEDURE s.uspSeq AS SELECT NEXT VALUE FOR s.Seq AS n",
    "CREATE PROCEDURE s.uspTvp @lines s.Lines READONLY AS SELECT l.Id, l.Qty FROM @lines AS l",
    "CREATE PROCEDURE s.uspNoResult AS INSERT INTO s.Child (Id, ParentId) VALUES (1, 10)",
    "CREATE PROCEDURE s.uspTemp AS BEGIN CREATE TABLE #t (a int); INSERT #t VALUES (1); SELECT a FROM #t END",
    "CREATE PROCEDURE s.uspDynamic AS BEGIN DECLARE @sql nvarchar(100) = N'SELECT 1 AS one'; EXEC (@sql) END",
    "CREATE PROCEDURE s.uspHidden @p int = 1 WITH ENCRYPTION AS SELECT @p AS one",
    "CREATE PROCEDURE s.uspWrongCase AS SELECT p.Id FROM s.parent AS p",
    "SET QUOTED_IDENTIFIER OFF",
    "CREATE PROCEDURE s.uspQuotedOff AS SELECT \"abc\" AS x",
    "SET QUOTED_IDENTIFIER ON",
    "SET ANSI_NULLS OFF",
    "CREATE PROCEDURE s.uspAnsiOff AS SELECT p.Id FROM s.Parent AS p WHERE p.Note = NULL",
    "SET ANSI_NULLS ON",
    "CREATE USER [migration_extractor] FOR LOGIN [migration_extractor]",
    "GRANT VIEW DEFINITION TO [migration_extractor]",
    "GRANT SELECT ON sys.sql_expression_dependencies TO [migration_extractor]",
]
PROCEDURES = ["uspPlain", "uspCaller", "uspViews", "uspSeq", "uspTvp", "uspNoResult", "uspTemp", "uspDynamic",
              "uspHidden", "uspWrongCase", "uspQuotedOff", "uspAnsiOff"]
CANNOT = ["uspTemp", "uspDynamic", "uspHidden", "uspWrongCase"]


def _drop(conn):
    conn.query(f"IF DB_ID(N'{SHAPE_DB}') IS NOT NULL BEGIN ALTER DATABASE [{SHAPE_DB}] SET SINGLE_USER WITH "
               f"ROLLBACK IMMEDIATE; DROP DATABASE [{SHAPE_DB}]; END")


@pytest.fixture(scope="session")
def shape_database():
    with sa_connection() as master:
        _drop(master)
        master.query(f"CREATE DATABASE [{SHAPE_DB}] COLLATE Latin1_General_CS_AS")
    with sa_connection(SHAPE_DB) as conn:
        for batch in SHAPE_BATCHES:
            conn.query(batch)
    yield SHAPE_DB
    with sa_connection() as master:
        _drop(master)


@pytest.fixture
def source(shape_database):
    return sandbox_server(databases=(shape_database,))


def sandbox() -> engine.Sandbox:
    return engine.Sandbox(os.environ.get("MSSQL_HOST", "sqlserver"), 1433, "sa", os.environ["MSSQL_SA_PASSWORD"])


def scratch_databases() -> list[str]:
    with sa_connection() as master:
        return [r["name"] for r in master.query("SELECT name FROM sys.databases WHERE name LIKE 'migrt[_]sandbox[_]%'")]


def procedure(name: str) -> str:
    return f"{SHAPE_DB}.s.{name}"


def shape(snapshot, identity: str) -> list[tuple]:
    return [(c["name"], c["declared_type"], c["system_type"], c["max_length"], c["precision"], c["scale"],
             c["nullable"]) for c in snapshot.get(identity).fact("result_shape").value]


def unavailable(snapshot, identity: str) -> str:
    obj = snapshot.get(identity)
    assert not obj.has_fact("result_shape"), f"{identity} has a shape"
    return obj.fact("result_shape_unavailable").value


@pytest.fixture(scope="module")
def described(shape_database):
    """The scratch database's snapshot with its procedures described in the sandbox."""
    from conftest import sandbox_server as server

    eng = engine.Engagement(name=f"test_shapes_{os.getpid()}", control_server_dsn=os.environ["MIGRT_CONTROL_DSN"])
    try:
        yield engine.extract(eng, server(databases=(shape_database,)), sandbox=sandbox())
    finally:
        engine.delete_engagement_data(eng)


# ---------------------------------------------------------------- recreated and described

def test_procedures_are_recreated_in_the_sandbox_and_described(described):
    assert shape(described, procedure("uspPlain")) == [
        ("Id", "int", "int", 4, 10, 0, False),
        ("Code", "Code", "nvarchar", 20, 0, 0, False),        # an alias type keeps its name
        ("Note", "varchar", "varchar", 20, 0, 0, True),
        ("Price", "decimal", "decimal", 9, 10, 2, False)]
    assert [(c[0], c[2]) for c in shape(described, procedure("uspCaller"))] == [("d", "int"), ("Id", "int")]
    assert [(c[0], c[2]) for c in shape(described, procedure("uspViews"))] == [("Id", "int")]
    assert [(c[0], c[2]) for c in shape(described, procedure("uspSeq"))] == [("n", "bigint")]
    assert [(c[0], c[2]) for c in shape(described, procedure("uspTvp"))] == [("Id", "int"), ("Qty", "int")]


def test_a_procedure_with_no_result_set_is_described_as_having_none(described):
    assert described.get(procedure("uspNoResult")).fact("result_shape").value == []


def test_a_shape_from_a_live_extraction_has_provenance_read_from_the_server(described):
    p = described.get(procedure("uspPlain")).fact("result_shape").provenance
    assert p.kind == "server" and "sandbox" in p.detail and "sys.dm_exec_describe_first_result_set_for_object" in p.detail


def test_the_sandbox_holds_nothing_after_an_extraction(described):
    assert scratch_databases() == []


def test_the_clients_settings_decide_what_the_sandbox_describes(described):
    # QUOTED_IDENTIFIER OFF turns "abc" into a string literal; ON would make it a column name.
    assert [(c[0], c[2], c[3]) for c in shape(described, procedure("uspQuotedOff"))] == [("x", "varchar", 3)]
    assert [c[0] for c in shape(described, procedure("uspAnsiOff"))] == ["Id"]


def test_each_module_is_created_under_the_settings_stored_with_it(engagement, source):
    snapshot = engine.extract(engagement, source)
    sent: list[str] = []

    shapes.attach(snapshot, shapes.SandboxAddress(**dataclasses.asdict(sandbox())), statements=sent)

    def settings_before(name: str) -> str:
        [at] = [i for i, s in enumerate(sent) if re.match(r"\s*CREATE\s+PROCEDURE\s+\[?s\]?\.\[?" + name + r"\b", s)]
        return sent[at - 1]
    assert "SET ANSI_NULLS OFF" in settings_before("uspAnsiOff")
    assert "SET ANSI_NULLS ON" in settings_before("uspPlain")
    assert "SET QUOTED_IDENTIFIER OFF" in settings_before("uspQuotedOff")
    assert "SET QUOTED_IDENTIFIER ON" in settings_before("uspPlain")


def test_the_databases_default_collation_is_the_sandboxs(described):
    # The database is case-sensitive; s.parent is not s.Parent. The client's server refuses it too.
    assert "Invalid object name 's.parent'" in unavailable(described, procedure("uspWrongCase"))


# ---------------------------------------------------------------- could not be described

def test_a_procedure_sql_server_cannot_describe_says_so_with_the_reason(described):
    for name in ("uspTemp", "uspDynamic"):
        assert unavailable(described, procedure(name)).startswith("SQL Server could not describe it (error 115"), name
    assert "dynamic SQL" in unavailable(described, procedure("uspDynamic"))


def test_an_encrypted_procedure_cannot_be_recreated(described):
    assert "encrypted" in unavailable(described, procedure("uspHidden"))


def test_an_undescribed_procedure_fails_loudly_through_the_query_interface(described):
    reader = CatalogReader(described, names.resolve)
    for name in CANNOT:
        assert not reader.has_fact(procedure(name), "result_shape")
        with pytest.raises(KeyError):
            reader.value(procedure(name), "result_shape")


def test_every_procedure_has_a_shape_or_a_reason_never_neither_or_both(described):
    for name in PROCEDURES:
        obj = described.get(procedure(name))
        assert obj.has_fact("result_shape") != obj.has_fact("result_shape_unavailable"), name
    assert not [f for f in described.get(procedure("uspPlain")).facts if f.name == "result_shape_unavailable"]


def test_without_a_sandbox_every_procedure_says_it_was_not_described(engagement, source):
    snapshot = engine.extract(engagement, source)

    for name in PROCEDURES:
        reason = unavailable(snapshot, procedure(name))
        assert ("encrypted" if name == "uspHidden" else "no sandbox was given") in reason, name


def test_a_sandbox_that_cannot_be_reached_fails_loudly_and_stores_nothing(engagement, source):
    bad = engine.Sandbox(os.environ.get("MSSQL_HOST", "sqlserver"), 1433, "sa", "not-the-password")

    with pytest.raises(engine.SandboxUnavailable):
        engine.extract(engagement, source, sandbox=bad)
    assert scratch_databases() == []


# ---------------------------------------------------------------- the findings and the report

def test_the_findings_list_what_could_not_be_described_and_what_the_sandbox_could_not_recreate(described):
    found = engine.extraction_findings(described)["result_shapes"]

    assert found["procedures"] == {"total": len(PROCEDURES), "described": len(PROCEDURES) - len(CANNOT)}
    assert sorted(r["object"] for r in found["could_not_be_described"]) == sorted(procedure(n) for n in CANNOT)
    assert all(r["reason"] for r in found["could_not_be_described"])
    assert (found["described_in_sandbox"], found["described_from_export"]) == (len(PROCEDURES) - len(CANNOT), 0)
    assert found["not_recreated"] == [] and found["export_unmatched"] == []


def test_a_preview_run_reports_the_shapes_it_could_not_describe(engagement, extractor_server, tmp_path):
    result = engine.preview_run(engagement, extractor_server, ["RetailSample.sales.vOrderTotals"], tmp_path / "out",
                                sandbox=sandbox())

    data = json.loads(result.report_data_path.read_text(encoding="utf-8"))
    assert data["extraction"]["result_shapes"]["procedures"] == {"total": 17, "described": 13}
    undescribed = {r["object"] for r in data["extraction"]["result_shapes"]["could_not_be_described"]}
    [entry] = [d for d in data["degradations"] if d["kind"] == "reduced coverage" and "result shape" in d["text"]]
    assert "4 procedures" in entry["text"] and "skips the shape comparison" in entry["text"]
    assert len(undescribed) == 4


def test_the_command_line_takes_the_sandbox(engagement, tmp_path, capsys):
    host = os.environ.get("MSSQL_HOST", "sqlserver")
    code = cli.main(["preview", "--engagement", engagement.name, "--control-dsn", engagement.control_server_dsn,
                     "--database", "RetailSample", "--artefact", "RetailSample.sales.vOrderTotals",
                     "--sandbox", f"{host}:1433", "--out", str(tmp_path / "out")])

    assert code == 0, capsys.readouterr().err
    data = json.loads((tmp_path / "out" / "report" / "report-data.json").read_text(encoding="utf-8"))
    assert data["extraction"]["result_shapes"]["procedures"] == {"total": 17, "described": 13}


# ---------------------------------------------------------------- the corpus

# What SQL Server describes on the real corpus databases that the sandbox, recreating the catalog
# alone, does not. Full-text search is manual work by an earlier decision (spec section 6); a
# natively compiled procedure needs memory-optimized tables and types, which the catalog does not
# yet hold (issue #103, "Memory-optimized tables, types and natively compiled procedures").
CORPUS_GAPS = {
    "AdventureWorks2022.dbo.uspSearchCandidateResumes": "full-text search, which is manual work",
    "WideWorldImporters.Website.RecordColdRoomTemperatures": "error 41323",
}
CORPUS = ("RetailSample", "AdventureWorks2022", "WideWorldImporters")


def test_on_the_corpus_39_of_69_procedures_are_described_and_the_two_gaps_are_named(engagement):
    snapshot = engine.extract(engagement, sandbox_server(databases=CORPUS), sandbox=sandbox())

    procedures = [o for o in snapshot.objects if o.kind == "procedure"]
    described = {o.identity for o in procedures if o.has_fact("result_shape")}
    # The real servers describe 41 of the 69 (spec section 3); the sandbox describes those but the gaps.
    real = set()
    for database in CORPUS:
        with sa_connection(database) as conn:
            for r in conn.query(
                    "SELECT s.name AS sn, o.name AS n FROM sys.objects AS o JOIN sys.schemas AS s ON "
                    "s.schema_id = o.schema_id OUTER APPLY (SELECT TOP 1 error_number FROM "
                    "sys.dm_exec_describe_first_result_set_for_object(o.object_id, 0)) AS d "
                    "WHERE o.type = 'P' AND o.is_ms_shipped = 0 AND d.error_number IS NULL"):
                real.add(f"{database}.{r['sn']}.{r['n']}")
    assert (len(real), len(procedures)) == (41, 69)
    assert real - described == set(CORPUS_GAPS) and described <= real
    assert len(described) == 39
    for identity, why in CORPUS_GAPS.items():
        assert why in unavailable(snapshot, identity), identity
    assert engine.extraction_findings(snapshot)["result_shapes"]["procedures"] == {"total": 69, "described": 39}
    assert scratch_databases() == []


# ---------------------------------------------------------------- folders alone

RETAIL = engine.FolderSettings(collations={"RetailSample": "SQL_Latin1_General_CP1_CI_AS"}, time_zone="UTC",
                               version="2022", compatibility_levels={"RetailSample": 160})
RETAIL_FOLDERS = [engine.Folder(SAMPLE / "database", "RetailSample"), engine.Folder(SAMPLE / "artefacts", "RetailSample")]


def test_views_and_procedures_from_folders_are_described_with_assumed_provenance(engagement):
    snapshot = engine.extract(engagement, None, RETAIL_FOLDERS, RETAIL, sandbox=sandbox())

    view = snapshot.get("RetailSample.sales.vOrderTotals")
    assert view.has_fact("result_shape") and not view.has_fact("result_shape_unavailable")
    assert view.fact("result_shape").provenance.kind == "assumed"
    procs = [o for o in snapshot.objects if o.kind == "procedure" and o.has_fact("result_shape")]
    assert len(procs) == 13 and {o.fact("result_shape").provenance.kind for o in procs} == {"assumed"}


def test_a_folder_extraction_describes_what_a_live_one_reads(engagement):
    live = engine.extract(engagement, sandbox_server(databases=("RetailSample",)), sandbox=sandbox())
    files = engine.extract(engagement, None, RETAIL_FOLDERS, RETAIL, sandbox=sandbox())

    compared = 0
    for obj in live.objects:
        if obj.kind in ("view", "inline_function", "table_function") and files.has(obj.identity):
            assert shape(files, obj.identity) == shape(live, obj.identity), obj.identity
            compared += 1
    for obj in live.objects:
        if obj.kind == "procedure":
            assert obj.has_fact("result_shape") == files.get(obj.identity).has_fact("result_shape"), obj.identity
            if obj.has_fact("result_shape"):
                assert shape(files, obj.identity) == shape(live, obj.identity), obj.identity
                compared += 1
    assert compared > 10


def test_without_a_sandbox_views_from_folders_say_they_were_not_described(engagement):
    snapshot = engine.extract(engagement, None, RETAIL_FOLDERS, RETAIL)

    view = snapshot.get("RetailSample.sales.vOrderTotals")
    assert not view.has_fact("result_shape") and "no sandbox was given" in view.fact("result_shape_unavailable").value


def test_the_compatibility_level_is_the_clients(engagement, tmp_path):
    (tmp_path / "split.sql").write_text(
        "CREATE PROCEDURE dbo.uspSplit AS SELECT value FROM STRING_SPLIT('a,b', ',')\n", encoding="utf-8")

    def extract(level):
        settings = engine.FolderSettings({"Db": "Latin1_General_CI_AS"}, "UTC", "2022", {"Db": level})
        return engine.extract(engagement, None, [engine.Folder(tmp_path, "Db")], settings, sandbox=sandbox())

    assert [c[0] for c in shape(extract(160), "Db.dbo.uspSplit")] == ["value"]
    assert "STRING_SPLIT" in unavailable(extract(110), "Db.dbo.uspSplit")   # not a function before level 130


def test_a_level_no_sandbox_accepts_is_not_described_and_says_why(engagement, tmp_path):
    (tmp_path / "p.sql").write_text("CREATE PROCEDURE dbo.uspOne AS SELECT 1 AS one\n", encoding="utf-8")
    settings = engine.FolderSettings({"Db": "Latin1_General_CI_AS"}, "UTC", "2022", {"Db": 90})

    snapshot = engine.extract(engagement, None, [engine.Folder(tmp_path, "Db")], settings, sandbox=sandbox())

    reason = unavailable(snapshot, "Db.dbo.uspOne")
    assert "compatibility level 90" in reason and "no sandbox accepts" in reason and "export" in reason
    assert scratch_databases() == []


# ---------------------------------------------------------------- the administrator's export

def export_script() -> str:
    text = ANNEX_F.read_text(encoding="utf-8")
    after = text[text.index("Export of result shapes"):]
    [script] = re.findall(r"```sql\n(.*?)```", after, re.S)[:1]
    return script.replace("<database>", SHAPE_DB)


def export_csv(database: str = SHAPE_DB, keep=lambda row: True, delimiter: str = ",") -> str:
    """What the administrator's script writes, saved as CSV from their own account."""
    with sa_connection(database) as conn:
        columns, rows = conn.capture(export_script())
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n", delimiter=delimiter)
    writer.writerow(columns)
    writer.writerows(["" if v is None else v for v in r.values()] for r in rows if keep(r))
    return out.getvalue()


def folder_with(tmp_path, **files) -> engine.Folder:
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return engine.Folder(tmp_path, SHAPE_DB)


def test_the_annex_script_runs_and_describes_every_procedure(shape_database):
    rows = list(csv.DictReader(io.StringIO(export_csv())))

    assert {r["procedure_name"] for r in rows} == set(PROCEDURES)
    assert {"schema_name", "procedure_name", "column_ordinal", "column_name", "system_type_name", "user_type_name",
            "max_length", "precision", "scale", "is_nullable", "error_number", "error_message"} <= set(rows[0])


def test_an_administrators_export_is_accepted_as_a_folder_input(engagement, source, described, tmp_path):
    folder = folder_with(tmp_path, **{"result-shapes.csv": export_csv()})

    snapshot = engine.extract(engagement, source, [folder])   # no sandbox: the export alone gives the shapes

    for name in ("uspPlain", "uspCaller", "uspViews", "uspSeq", "uspTvp", "uspNoResult", "uspQuotedOff", "uspAnsiOff"):
        assert snapshot.get(procedure(name)).fact("result_shape").value ==             described.get(procedure(name)).fact("result_shape").value, name
    p = snapshot.get(procedure("uspPlain")).fact("result_shape").provenance
    assert (p.kind, p.detail) == ("file", f"{SHAPE_DB}/result-shapes.csv")
    # The server describes what the sandbox cannot recreate: an encrypted procedure has a shape.
    assert [(c[0], c[2]) for c in shape(snapshot, procedure("uspHidden"))] == [("one", "int")]
    # Where the administrator's own server could not describe one, the file says so, with its reason.
    reason = unavailable(snapshot, procedure("uspDynamic"))
    assert "dynamic SQL" in reason and "result-shapes.csv" in reason
    assert snapshot.get(procedure("uspDynamic")).fact("result_shape_unavailable").provenance.kind == "file"
    raw = snapshot.get(f"raw:file:{SHAPE_DB}/result-shapes.csv")
    assert raw.kind == "raw_capture"


def test_the_export_takes_the_place_of_the_sandbox_for_what_it_covers(engagement, source, tmp_path):
    folder = folder_with(tmp_path, **{"result-shapes.csv": export_csv(keep=lambda r: r["procedure_name"] != "uspPlain")})

    snapshot = engine.extract(engagement, source, [folder], sandbox=sandbox())

    assert snapshot.get(procedure("uspCaller")).fact("result_shape").provenance.kind == "file"
    assert snapshot.get(procedure("uspPlain")).fact("result_shape").provenance.kind == "server"   # the sandbox's
    found = engine.extraction_findings(snapshot)["result_shapes"]
    assert found["described_from_export"] > 0 and found["described_in_sandbox"] > 0


def test_the_export_gives_shapes_where_no_sandbox_accepts_the_level(engagement, tmp_path):
    (tmp_path / "p.sql").write_text("CREATE PROCEDURE dbo.uspOne AS SELECT 1 AS one\n", encoding="utf-8")
    export = ("schema_name,procedure_name,column_ordinal,column_name,system_type_name,user_type_schema,"
              "user_type_name,max_length,precision,scale,is_nullable,error_number,error_message\r\n"
              "dbo,uspOne,1,one,int,,,4,10,0,0,,\r\n")
    (tmp_path / "shapes.csv").write_text(export, encoding="utf-8")
    settings = engine.FolderSettings({"Db": "Latin1_General_CI_AS"}, "UTC", "2022", {"Db": 90})

    snapshot = engine.extract(engagement, None, [engine.Folder(tmp_path, "Db")], settings, sandbox=sandbox())

    assert shape(snapshot, "Db.dbo.uspOne") == [("one", "int", "int", 4, 10, 0, False)]
    assert snapshot.get("Db.dbo.uspOne").fact("result_shape").provenance == Provenance("file", "Db/shapes.csv")


def test_an_export_entry_for_a_procedure_the_catalog_lacks_is_reported_never_dropped(engagement, source, tmp_path):
    rows = export_csv().splitlines()
    ghost = "s,uspGhost,1,g,int,,,4,10,0,0,,"
    folder = folder_with(tmp_path, **{"result-shapes.csv": "\r\n".join([*rows, ghost]) + "\r\n"})

    snapshot = engine.extract(engagement, source, [folder])

    found = engine.extraction_findings(snapshot)["result_shapes"]
    assert found["export_unmatched"] == [{"file": f"{SHAPE_DB}/result-shapes.csv", "object": f"{SHAPE_DB}.s.uspGhost"}]


def test_a_csv_that_is_not_an_export_is_listed_as_not_read(engagement, source, tmp_path):
    folder = folder_with(tmp_path, **{"notes.csv": "a,b\n1,2\n"})

    snapshot = engine.extract(engagement, source, [folder])

    [entry] = [f for f in engine.extraction_findings(snapshot)["folders"]["files"] if f["file"].endswith("notes.csv")]
    assert entry["role"] == folders.NOT_READ


def test_a_malformed_export_is_an_extraction_error_naming_its_file_and_line(engagement, source, tmp_path):
    text = export_csv().splitlines()
    text[1] = text[1].replace(",1,", ",first,", 1)
    folder = folder_with(tmp_path, **{"result-shapes.csv": "\r\n".join(text) + "\r\n"})

    with pytest.raises(engine.FolderExtractionError, match=r"result-shapes\.csv.*line 2"):
        engine.extract(engagement, source, [folder])


def test_an_export_entry_for_an_object_that_is_not_a_procedure_is_reported_too(engagement, source, tmp_path):
    rows = export_csv().splitlines()
    folder = folder_with(tmp_path, **{"result-shapes.csv": "\r\n".join([*rows, "s,vParents,1,Id,int,,,4,10,0,0,,"]) + "\r\n"})

    snapshot = engine.extract(engagement, source, [folder])

    found = engine.extraction_findings(snapshot)["result_shapes"]
    assert found["export_unmatched"] == [{"file": f"{SHAPE_DB}/result-shapes.csv", "object": f"{SHAPE_DB}.s.vParents"}]
    assert not snapshot.get(f"{SHAPE_DB}.s.vParents").has_fact("result_shape_unavailable")


def test_a_csv_that_is_not_text_is_listed_as_not_read_with_its_own_reason(engagement, source, tmp_path):
    (tmp_path / "blob.csv").write_bytes(b"\xff\xfe\x00\xd8garbage\x80")
    
    snapshot = engine.extract(engagement, source, [engine.Folder(tmp_path, SHAPE_DB)])

    [entry] = [f for f in engine.extraction_findings(snapshot)["folders"]["files"] if f["file"].endswith("blob.csv")]
    assert entry["role"] == folders.NOT_READ and "not text" in entry["reason"]
