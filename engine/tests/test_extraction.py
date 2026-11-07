"""The extractor's self-check and the snapshot, through the engine's library interface."""
import os
import uuid

import pytest

from migrt import engine
from migrt.sources.sqlserver.connection import Connection

from conftest import sandbox_server

VIEW = "RetailSample.sales.vOrderTotals"


@pytest.fixture
def dba():
    """sa, playing the client's DBA, to set up logins the tests need."""
    with Connection(os.environ.get("MSSQL_HOST", "sqlserver"), 1433, "sa", os.environ["MSSQL_SA_PASSWORD"]) as conn:
        yield conn


@pytest.fixture
def make_login(dba):
    """Logins with a user in RetailSample and the msdb grants, plus whatever a test adds;
    each is dropped afterwards."""
    made = []

    def make(view_definition=True, extra=""):
        name, password = f"t_{uuid.uuid4().hex[:8]}", "Pw1!" + uuid.uuid4().hex
        made.append(name)
        dba.query(f"CREATE LOGIN [{name}] WITH PASSWORD = '{password}'")
        dba.query(f"USE RetailSample; CREATE USER [{name}] FOR LOGIN [{name}];" +
                  (f"GRANT VIEW DEFINITION TO [{name}];" if view_definition else ""))
        dba.query(f"USE msdb; CREATE USER [{name}] FOR LOGIN [{name}];" +
                  "".join(f"GRANT SELECT ON dbo.{t} TO [{name}];"
                          for t in ("sysjobs", "sysjobsteps", "sysschedules", "sysjobschedules", "syscategories")))
        if extra:
            dba.query(extra.format(name=name))
        return name, password

    yield make
    for name in made:
        dba.query(f"USE RetailSample; DROP USER [{name}]; USE msdb; DROP USER [{name}]; USE master; DROP LOGIN [{name}]")


def test_missing_view_definition_fails_loudly_before_anything_is_read(engagement, make_login, tmp_path):
    name, password = make_login(view_definition=False)

    with pytest.raises(engine.ExtractorPermissionError) as raised:
        engine.preview_run(engagement, sandbox_server(name, password), artefacts=[VIEW], out_dir=tmp_path)

    assert raised.value.missing == ["database RetailSample: VIEW DEFINITION"]
    assert "VIEW DEFINITION" in str(raised.value)
    assert not (tmp_path / "converted").exists()


def test_a_login_holding_more_than_read_only_needs_is_refused(engagement, tmp_path):
    sa = sandbox_server("sa", os.environ["MSSQL_SA_PASSWORD"])

    with pytest.raises(engine.ExtractorPermissionError) as raised:
        engine.extract(engagement, sa)

    assert any("CONTROL SERVER" in e for e in raised.value.excess)


def test_rows_readable_through_a_role_are_refused(engagement, make_login):
    name, password = make_login(extra="USE RetailSample; ALTER ROLE db_datareader ADD MEMBER [{name}]")

    with pytest.raises(engine.ExtractorPermissionError) as raised:
        engine.extract(engagement, sandbox_server(name, password))

    assert "database RetailSample: SELECT on [sales].[Order]" in raised.value.excess


def test_an_agent_role_in_msdb_is_refused(engagement, make_login):
    name, password = make_login(extra="USE msdb; ALTER ROLE SQLAgentReaderRole ADD MEMBER [{name}]")

    with pytest.raises(engine.ExtractorPermissionError) as raised:
        engine.extract(engagement, sandbox_server(name, password))

    assert "database msdb: member of SQLAgentReaderRole" in raised.value.excess


def test_every_fact_in_a_snapshot_carries_provenance(engagement, extractor_server):
    snapshot = engine.extract(engagement, extractor_server)
    stored = engine.read_snapshot(engagement, snapshot.id)

    facts = [f for o in stored.objects for f in o.facts]
    assert facts and all(f.provenance.kind == "server" and f.provenance.detail for f in facts)
    view = stored.get(VIEW)
    assert "CREATE VIEW sales.vOrderTotals" in view.fact("source_text").value
    assert view.fact("source_text").provenance.detail == "sys.views, sys.sql_modules"
    order = stored.get("RetailSample.sales.Order")
    assert [c["name"] for c in order.fact("columns").value] == [
        "OrderId", "CustomerId", "OrderDate", "ShippedAt", "Status", "Note", "TotalAmount"]


def test_re_extracting_an_unchanged_server_gives_the_same_snapshot(engagement, extractor_server):
    first = engine.extract(engagement, extractor_server)
    second = engine.extract(engagement, extractor_server)

    assert first.id == second.id
    assert [o.content_hash for o in first.objects] == [o.content_hash for o in second.objects]
