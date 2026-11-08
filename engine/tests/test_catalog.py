"""Snapshot identity (ADR 0023): the identifier is the hash of the content, and provenance
is part of the content, so extractions that differ only in provenance never share one."""
import pytest

from migrt.core.catalog import (IGNORED_FOR_IDENTITY, CatalogObject, CatalogReader, Fact,
                               IgnoredCellsRead, Provenance, Resolution, Snapshot)

COLUMNS = [{"name": "OrderId", "system_type": "int", "nullable": False}]


def snapshot_with(provenance):
    return Snapshot.of("sqlserver", [CatalogObject("RetailSample.sales.Order", "table",
                                                   (Fact("skeleton", "columns", COLUMNS, provenance),))])


def test_the_same_facts_with_the_same_provenance_give_the_same_snapshot():
    read = Provenance("server", "sys.tables, sys.columns")
    assert snapshot_with(read).id == snapshot_with(read).id


def test_a_live_and_a_folder_extraction_of_the_same_facts_never_share_an_identifier():
    live = snapshot_with(Provenance("server", "sys.tables, sys.columns"))
    folder = snapshot_with(Provenance("file", "tables/sales.Order.sql"))
    assumed = snapshot_with(Provenance("assumed", "database default collation setting"))

    assert len({live.id, folder.id, assumed.id}) == 3


def test_a_different_provenance_detail_alone_gives_a_different_snapshot():
    one = snapshot_with(Provenance("file", "export-a/sales.Order.sql"))
    other = snapshot_with(Provenance("file", "export-b/sales.Order.sql"))
    assert one.id != other.id


def raw(log_wait, compatibility=160):
    read = Provenance("server", "sys.databases")
    return Snapshot.of("sqlserver", [CatalogObject("raw:db:Shop/sys.databases", "raw_capture", (
        Fact("skeleton", "rows", [{"name": "Shop", "log_reuse_wait": log_wait, "compatibility_level": compatibility}],
             read),
        Fact("skeleton", IGNORED_FOR_IDENTITY, {"fact": "rows", "columns": ["log_reuse_wait"]}, read),
    ))])


def test_cells_the_identity_ignores_leave_the_snapshot_the_same_but_not_the_content():
    """ADR 0023, amended: columns the server changes by itself."""
    first, second = raw(0), raw(2)
    assert first.id == second.id
    assert first.objects[0].content_hash != second.objects[0].content_hash
    assert raw(0, compatibility=150).id != first.id


def test_no_reader_can_read_a_fact_holding_ignored_cells():
    reader = CatalogReader(raw(0), lambda snapshot, parts, context: Resolution(None))
    with pytest.raises(IgnoredCellsRead):
        reader.value("raw:db:Shop/sys.databases", "rows")
    assert reader.reads == []
