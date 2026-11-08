"""How conversion reads the catalog (spec section 3): only through the core's query
interface, which records every read, with SQL Server's name resolver supplying default
schemas, case-insensitive identifiers and synonyms."""
import pytest

from migrt.core.catalog import CatalogObject, CatalogReader, Fact, Provenance, Snapshot
from migrt.sources.sqlserver import names

READ = Provenance("server", "test")


def obj(identity, kind, **facts):
    return CatalogObject(identity, kind, tuple(Fact("skeleton", k, v, READ) for k, v in facts.items()))


SNAPSHOT = Snapshot.of("sqlserver", [
    obj("Shop", "database", collation="Latin1_General_CI_AS"),
    obj("Shop.dbo.Customer", "table", columns=[{"name": "Id"}]),
    obj("Shop.sales.Customer", "table", columns=[{"name": "Id"}, {"name": "Region"}]),
    obj("Shop.sales.Order", "table", columns=[]),
    obj("Shop.dbo.Orders", "synonym", base_object=["Shop", "sales", "Order"]),
    obj("Shop.dbo.Remote", "synonym", base_object=["Other", "dbo", "Thing"]),
    obj("Shop.sales.vOrders", "view", references=[{"name": ["Order"], "resolved": "Shop.sales.Order"}]),
    obj("Shop.sales.vViaSynonym", "view", references=[{"name": ["dbo", "Orders"], "resolved": "Shop.sales.Order",
                                                        "via": "Shop.dbo.Orders"}]),
    obj("Shop.sales.uspClose", "procedure", references=[{"name": ["dbo", "Orders"], "resolved": "Shop.sales.Order"}]),
    obj("Shop.sales.trOrder", "trigger", table="Shop.sales.Order"),
    obj("Shop.sales.trCustomer", "trigger", table="Shop.sales.Customer"),
    obj("type:Shop.dbo.Customer", "user_type", base_type="int"),
])


def reader():
    return CatalogReader(SNAPSHOT, names.resolve)


def context(**kw):
    return {"database": "Shop", "schemas": ["sales", "dbo"], "case_sensitive": False, **kw}


def test_a_one_part_name_is_looked_for_in_the_modules_schema_then_dbo():
    assert reader().resolve(("Customer",), context()) == "Shop.sales.Customer"
    assert reader().resolve(("Customer",), context(schemas=["dbo"])) == "Shop.dbo.Customer"


def test_an_omitted_schema_in_a_three_part_name_is_looked_for_as_a_one_part_name_is():
    assert reader().resolve(("Shop", "", "Customer"), context()) == "Shop.sales.Customer"


def test_identifiers_compare_as_the_database_collation_does():
    assert reader().resolve(("SALES", "customer"), context()) == "Shop.sales.Customer"
    assert reader().resolve(("SALES", "customer"), context(case_sensitive=True)) is None


def test_a_synonym_resolves_to_its_base_object_and_both_are_read():
    r = reader()
    assert r.resolve(("dbo", "Orders"), context(kinds={"table"})) == "Shop.sales.Order"
    assert [read.identity for read in r.reads] == ["Shop.dbo.Orders", "Shop.sales.Order"]


def test_a_synonym_for_an_object_outside_the_catalog_resolves_to_the_synonym():
    assert reader().resolve(("Remote",), context(schemas=["dbo"])) == "Shop.dbo.Remote"


def test_types_have_their_own_namespace():
    assert reader().resolve(("dbo", "Customer"), context(kinds={"user_type"})) == "type:Shop.dbo.Customer"
    assert reader().resolve(("dbo", "Customer"), context()) == "Shop.dbo.Customer"


def test_what_refers_to_an_object_is_answered_and_every_object_consulted_is_read():
    r = reader()
    assert r.referrers("Shop.sales.Order") == ["Shop.sales.uspClose", "Shop.sales.vOrders", "Shop.sales.vViaSynonym"]
    assert {(read.identity, read.fact) for read in r.reads} == {
        ("Shop.sales.uspClose", "references"), ("Shop.sales.vOrders", "references"),
        ("Shop.sales.vViaSynonym", "references")}
    assert reader().referrers("Shop.dbo.Orders") == ["Shop.sales.vViaSynonym"]


def test_the_triggers_on_a_table_are_answered_and_read():
    r = reader()
    assert r.triggers_on("Shop.sales.Order") == ["Shop.sales.trOrder"]
    assert {(read.identity, read.fact) for read in r.reads} == {
        ("Shop.sales.trOrder", "table"), ("Shop.sales.trCustomer", "table")}


def test_a_changed_fact_that_was_read_changes_the_reads_hash_and_one_not_read_does_not():
    def changed(identity, **facts):
        objects = [o if o.identity != identity else obj(identity, o.kind, **facts) for o in SNAPSHOT.objects]
        return CatalogReader(Snapshot.of("sqlserver", objects), names.resolve)

    def reads_columns(r):
        r.value(r.resolve(("Customer",), context()), "columns")
        return r.reads_hash()

    base = reads_columns(reader())
    assert reads_columns(changed("Shop.dbo.Customer", columns=[{"name": "Other"}])) == base
    assert reads_columns(changed("Shop.sales.Customer", columns=[{"name": "Id"}])) != base
    # Another fact of the same object, such as a row count that moves with the data, was not read.
    assert reads_columns(changed("Shop.sales.Customer", columns=[{"name": "Id"}, {"name": "Region"}],
                                 row_count=42)) == base


def test_an_ambiguous_name_is_refused_never_guessed():
    snapshot = Snapshot.of("sqlserver", [obj("Shop.dbo.T", "table"), obj("Shop.dbo.t", "table")])
    with pytest.raises(names.AmbiguousName):
        CatalogReader(snapshot, names.resolve).resolve(("T",), context(schemas=["dbo"]))
