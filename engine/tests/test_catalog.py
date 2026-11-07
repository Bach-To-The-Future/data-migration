"""Snapshot identity (ADR 0023): the identifier is the hash of the content, and provenance
is part of the content, so extractions that differ only in provenance never share one."""
from migrt.core.catalog import CatalogObject, Fact, Provenance, Snapshot

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
