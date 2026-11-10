"""The references in constraint, default and computed-column expressions are worked out by the parser, and
cross-checked against SQL Server's dependency metadata like a module's (issue #42, knock-on of issue #39)."""
from migrt.sources.sqlserver import extractor

from conftest import sa_connection, sandbox_server


def check_of(snapshot, identity):
    return snapshot.get(identity).fact("dependency_cross_check", extractor.SOURCE).value


def test_an_expression_that_calls_a_function_is_checked_and_agrees(scratch_database):
    with sa_connection(scratch_database) as conn:
        conn.query("CREATE FUNCTION s.fnTax (@a int) RETURNS int WITH SCHEMABINDING AS BEGIN RETURN @a / 10 END")
        conn.query("CREATE TABLE s.Priced (Amount int NOT NULL, Tax AS (s.fnTax(Amount)), "
                   "Flag int NOT NULL CONSTRAINT DF_Priced_Flag DEFAULT (s.fnTax(1)), "
                   "CONSTRAINT CK_Priced CHECK (s.fnTax(Amount) >= 0))")
    try:
        snapshot = extractor.extract(extractor.ServerAddress(sandbox_server().host, 1433, "migration_extractor",
                                                             sandbox_server().password), [scratch_database])
    finally:
        with sa_connection(scratch_database) as conn:
            conn.query("DROP TABLE s.Priced")
            conn.query("DROP FUNCTION s.fnTax")

    priced = f"{scratch_database}.s.Priced"
    check = check_of(snapshot, priced)
    assert (check["status"], check["parser_only"], check["server_only"]) == ("agrees", [], [])
    assert snapshot.get(priced).fact("server_dependencies", extractor.SOURCE).value   # the function was recorded
    assert extractor.findings(snapshot)["dependency_cross_check"]["disagreements"] == []


def test_every_corpus_table_with_expression_dependencies_is_cross_checked_and_agrees(corpus_snapshot):
    found = extractor.findings(corpus_snapshot)["dependency_cross_check"]
    holders = [o for o in corpus_snapshot.objects if o.has_fact("server_dependencies", extractor.SOURCE)
               and o.kind in ("table", "table_type")]

    assert len(holders) == 62        # issue #39 counted 63 objects with a cross-check that did not run
    assert [h.identity for h in holders if check_of(corpus_snapshot, h.identity)["status"] != "agrees"] == []
    # What remains "not run" has no extracted definition to read: a security policy, with its reason.
    kinds = {corpus_snapshot.get(n["object"]).kind for n in found["not_run"]}
    assert kinds == {"other_object"} and all("definition is not extracted" in n["reason"] for n in found["not_run"])
