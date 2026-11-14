"""The extractor's self-check, through the engine's library interface."""
import os
import uuid

import pytest

from migrt import engine
from migrt.sources.sqlserver.connection import Connection

from conftest import RUN, sa_connection, sandbox_server

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
    assert view.fact("source_text").provenance.detail == "sys.sql_modules"
    order = stored.get("RetailSample.sales.Order")
    assert [c["name"] for c in order.fact("columns").value] == [
        "OrderId", "CustomerId", "OrderDate", "ShippedAt", "Status", "Note", "TotalAmount"]


def cells_of(fact_value) -> dict[tuple, object]:
    """The cells of a fact's value, named by position: a list of rows is (row, column) cells, anything else one cell."""
    if isinstance(fact_value, list) and all(isinstance(r, dict) for r in fact_value):
        return {(i, c): v for i, row in enumerate(fact_value) for c, v in row.items()}
    return {(): fact_value}


def differing_cells(first, second):
    """Every difference between two extractions' full content, as (covered, uncovered): the cells that differ in a cell the
    ignored lists name (ADR 0023: a raw capture's ignored columns, an informational fact as a whole), and every other
    differing cell, each named by object, fact and cell. Written apart from the identity's own hashing."""
    covered, uncovered = [], []
    a_by, b_by = {o.identity: o for o in first.objects}, {o.identity: o for o in second.objects}
    for identity in sorted(a_by.keys() | b_by.keys()):
        if identity not in a_by or identity not in b_by:
            uncovered.append(f"{identity}: the object is in only one extraction")
            continue
        a, b = a_by[identity], b_by[identity]
        ignored = {e["fact"]: e.get("columns") for e in a.fact("ignored_for_identity").value}             if a.has_fact("ignored_for_identity") else {}
        a_facts, b_facts = {(f.area, f.name): f for f in a.facts}, {(f.area, f.name): f for f in b.facts}
        for key in sorted(a_facts.keys() | b_facts.keys()):
            area, name = key
            if key not in a_facts or key not in b_facts:
                uncovered.append(f"{identity}: the fact {name} is in only one extraction")
                continue
            fa, fb = a_facts[key], b_facts[key]
            if fa.provenance != fb.provenance:
                uncovered.append(f"{identity}.{name}: its provenance differs")
            if fa.value == fb.value:
                continue
            if area == "skeleton" and name in ignored and ignored[name] is None:
                covered.append(f"{identity}.{name}")                    # an informational fact, left out as a whole
                continue
            ca, cb = cells_of(fa.value), cells_of(fb.value)
            for cell in sorted(ca.keys() | cb.keys(), key=str):
                if ca.get(cell) == cb.get(cell) and (cell in ca) == (cell in cb):
                    continue
                column = cell[1] if len(cell) == 2 else None
                in_ignored = area == "skeleton" and name in ignored and column in (ignored[name] or ())                     and cell in ca and cell in cb
                (covered if in_ignored else uncovered).append(f"{identity}.{name}{list(cell) if cell else ''}")
    return covered, uncovered


def test_re_extracting_an_unchanged_server_gives_the_same_snapshot(engagement, extractor_server):
    first = engine.extract(engagement, extractor_server)
    second = engine.extract(engagement, extractor_server)

    assert first.id == second.id
    # What the identity leaves out (ADR 0023) may differ between two reads of an unchanged server: Query Store keeps moving
    # the row counts of RetailSample's partitions. Whatever differs must lie in a cell the ignored lists name, and any
    # other differing cell fails the test by name.
    covered, uncovered = differing_cells(first, second)
    assert uncovered == []
    assert [o.identity_hash for o in first.objects] == [o.identity_hash for o in second.objects]


def test_a_difference_in_a_cell_the_ignored_lists_do_not_name_is_found_and_named():
    from migrt.core.catalog import CatalogObject, Fact, Provenance, Snapshot

    p = Provenance("server", "test")

    def snapshot(rows, count):
        facts = (Fact("skeleton", "rows", rows, p), Fact("skeleton", "row_count", count, p),
                 Fact("skeleton", "ignored_for_identity", [{"fact": "rows", "columns": ["moves"]}, {"fact": "row_count"}], p))
        return Snapshot.of("test", [CatalogObject("raw:db:X/sys.t", "raw_capture", facts)])

    base = snapshot([{"id": 1, "moves": 5, "stays": "a"}], 10)
    moved = snapshot([{"id": 1, "moves": 9, "stays": "a"}], 11)
    changed = snapshot([{"id": 1, "moves": 9, "stays": "b"}], 11)
    assert differing_cells(base, moved) == (["raw:db:X/sys.t.row_count", "raw:db:X/sys.t.rows[0, 'moves']"], [])
    assert differing_cells(base, changed)[1] == ["raw:db:X/sys.t.rows[0, 'stays']"]


def test_a_database_created_between_two_extractions_leaves_the_snapshot_identity_unchanged(engagement, extractor_server, dba):
    name = f"MigrtUnrelated_{RUN}"
    first = engine.extract(engagement, extractor_server)
    dba.query(f"IF DB_ID(N'{name}') IS NOT NULL DROP DATABASE [{name}]; CREATE DATABASE [{name}]")
    try:
        second = engine.extract(engagement, extractor_server)
    finally:
        dba.query(f"DROP DATABASE [{name}]")
    assert first.id == second.id


def test_queries_run_against_the_database_between_two_extractions_leave_the_snapshot_identity_unchanged(engagement, extractor_server):
    """Query Store writes to its internal tables while queries run (a new collection interval adds rows), which can move the
    raw capture of sys.partitions."""
    first = engine.extract(engagement, extractor_server)
    with sa_connection("RetailSample") as retail:           # Query Store records a query in the database it runs in
        for i in range(30):
            retail.query(f"SELECT COUNT(*) AS c FROM sales.[Order] AS o JOIN sales.OrderLine AS l "
                         f"ON l.OrderId = o.OrderId WHERE o.OrderId > {i} AND '{RUN}' = '{RUN}'")     # new text each run
        retail.query("EXEC sys.sp_query_store_flush_db")
    second = engine.extract(engagement, extractor_server)
    assert first.id == second.id
    covered, uncovered = differing_cells(first, second)
    assert uncovered == []
    # Whether Query Store moves a count here depends on its collection interval, so the test cannot demand it; the test above
    # shows that a cell the lists do not name is found. What it holds is that whatever moved is a cell the lists name.
