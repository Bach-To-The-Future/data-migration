"""Lowering of cursors (issue #85): `declare_cursor`, `open_cursor`, `fetch_cursor`, `fetch_status`, `close_cursor`,
`deallocate_cursor` and `write_current_row`, and what they do to the scope. A cursor is lowered as a cursor: raising
it to a set-based step is optional and comes later."""
import pathlib

from procedural_helpers import (checked, kinds, lowered, lowered_script, opaque_statements, proc, scope_of,
                                statements)

from lowering_helpers import lower_text, of_kind

SAMPLES = pathlib.Path(__file__).parents[2] / "corpus" / "sample" / "artefacts" / "procedures"
CURSOR = ("DECLARE @id int, @status tinyint;\n"
          "DECLARE c CURSOR LOCAL FAST_FORWARD FOR SELECT o.OrderId, o.Status FROM sales.[Order] AS o ORDER BY o.OrderId;\n"
          "OPEN c;\nFETCH NEXT FROM c INTO @id, @status;\nWHILE @@FETCH_STATUS = 0\nBEGIN\n"
          "  FETCH NEXT FROM c INTO @id, @status;\nEND\nCLOSE c;\nDEALLOCATE c;")


def test_the_cursor_life_cycle_lowers_to_one_operation_per_statement(corpus_snapshot):
    artefact = lowered(corpus_snapshot, CURSOR)

    *_, declare, open_, fetch, loop, close, deallocate = statements(artefact.body)
    assert kinds([declare, open_, fetch, loop, close, deallocate]) == [
        "declare_cursor", "open_cursor", "fetch_cursor", "while", "close_cursor", "deallocate_cursor"]
    assert not opaque_statements(artefact)
    assert declare.attrs["name"] == "c"
    assert [declare.attrs[k] for k in ("cursor_scope", "kind", "read_only")] == ["local", "fast_forward", True]
    assert open_.attrs["cursor"] == close.attrs["cursor"] == deallocate.attrs["cursor"] == "c"


def test_a_cursor_keeps_its_query_and_names_the_columns_it_returns(corpus_snapshot):
    artefact = lowered(corpus_snapshot, CURSOR)

    [declare] = of_kind(artefact, "declare_cursor")
    query = declare.child("query")
    assert query.kind == "sort"            # ORDER BY is kept: a fetch reads rows in the query's order
    assert [c["name"] for c in declare.attrs["columns"]] == ["OrderId", "Status"]
    assert [c["type"]["name"] for c in declare.attrs["columns"]] == ["integer", "integer"]


def test_a_fetch_names_its_direction_and_the_variables_with_the_conversion_to_each_type(corpus_snapshot):
    artefact = lowered(corpus_snapshot, CURSOR)

    [first, second] = of_kind(artefact, "fetch_cursor")
    assert first.attrs["direction"] == "next" and first.attrs["cursor"] == "c"
    assert first.attrs["variables"] == ["@id", "@status"]
    assert [t["variable"] for t in first.attrs["targets"]] == ["@id", "@status"]
    # the column is a tinyint and the variable a tinyint; the id is an int in both
    assert all("from_type" in t and "to_type" in t for t in first.attrs["targets"])
    assert second.attrs["direction"] == "next"


def test_the_fetch_status_is_a_value_of_its_own(corpus_snapshot):
    artefact = lowered(corpus_snapshot, CURSOR)

    [loop] = of_kind(artefact, "while")
    status = [n for n in loop.child("condition").walk() if n.kind == "fetch_status"]
    assert len(status) == 1 and str(status[0].type) == "integer(bits=32, signed=True)"


def test_a_scrollable_fetch_keeps_its_direction_and_offset(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @id int;\nDECLARE c CURSOR LOCAL SCROLL STATIC FOR SELECT o.OrderId FROM sales.[Order] AS o;\n"
                       "OPEN c;\nFETCH ABSOLUTE 3 FROM c INTO @id;\nFETCH RELATIVE -1 FROM c INTO @id;\n"
                       "FETCH FIRST FROM c INTO @id;\nFETCH PRIOR FROM c INTO @id;\nFETCH LAST FROM c INTO @id;")

    declare = of_kind(artefact, "declare_cursor")[0]
    assert declare.attrs["scrollable"] is True and declare.attrs["kind"] == "static"
    absolute, relative, first, prior, last = of_kind(artefact, "fetch_cursor")
    assert [f.attrs["direction"] for f in (absolute, relative, first, prior, last)] == [
        "absolute", "relative", "first", "prior", "last"]
    assert absolute.child("offset").attrs["value"] == 3
    assert relative.child("offset").kind in ("literal", "negate")
    assert first.children_with("offset") == []


def test_a_cursor_in_scope_is_declared_then_open_then_closed_then_gone(corpus_snapshot):
    artefact = lowered(corpus_snapshot, CURSOR)

    *_, declare, open_, fetch, loop, close, deallocate = statements(artefact.body)
    after_deallocate = scope_of(deallocate)
    assert scope_of(declare)["cursors"] == [] and scope_of(declare)["open_cursors"] == []
    assert scope_of(open_)["cursors"] == ["c"] and scope_of(open_)["open_cursors"] == []
    assert scope_of(fetch)["open_cursors"] == ["c"]
    assert scope_of(close)["open_cursors"] == ["c"]
    assert scope_of(deallocate)["cursors"] == ["c"] and scope_of(deallocate)["open_cursors"] == []
    assert after_deallocate["cursors"] == ["c"]


def test_a_fetch_inside_a_loop_sees_the_cursor_open(corpus_snapshot):
    artefact = lowered(corpus_snapshot, CURSOR)

    inner = [f for f in of_kind(artefact, "fetch_cursor")][1]
    assert scope_of(inner)["open_cursors"] == ["c"]


def test_a_local_cursor_ends_with_its_batch_and_a_global_one_does_not(corpus_snapshot):
    artefact = lowered_script(corpus_snapshot,
                              "DECLARE a CURSOR LOCAL FOR SELECT 1 AS x;\nDECLARE b CURSOR GLOBAL FOR SELECT 1 AS x;\nGO\n"
                              "SELECT 1;\n")

    first, second = artefact.body.children_with("batch")
    [select] = statements(second)
    assert scope_of(select)["cursors"] == ["b"]


def test_a_cursor_without_local_or_global_takes_the_databases_default_and_says_so(corpus_snapshot):
    artefact = lowered(corpus_snapshot, "DECLARE c CURSOR FOR SELECT o.OrderId FROM sales.[Order] AS o;")

    [declare] = of_kind(artefact, "declare_cursor")
    assert declare.attrs["cursor_scope"] == "database_default"
    assert any("CURSOR_DEFAULT" in a for a in artefact.assumptions)


def test_closing_or_fetching_a_cursor_never_declared_is_a_checker_finding(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, "DECLARE c CURSOR LOCAL FOR SELECT 1 AS x;\nOPEN c;\nCLOSE c;")
    assert checked(artefact, reader) == []
    artefact.body.children = [c for c in artefact.body.children if c.kind != "declare_cursor"]
    findings = checked(artefact, reader)
    assert any("cursor c" in f.message and "scope" in f.message for f in findings)


def test_a_fetch_into_a_variable_not_in_scope_is_kept_as_written(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, "DECLARE c CURSOR LOCAL FOR SELECT 1 AS x;\nOPEN c;\nFETCH NEXT FROM c INTO @missing;")
    assert checked(artefact, reader) == []

    [fragment] = opaque_statements(artefact)
    assert "@missing" in fragment.attrs["reason"] and fragment.attrs["scope_barrier"] is False
    assert fragment.attrs["touches"]["undeclared"] == ["@missing"]


def test_a_fetch_into_a_different_number_of_variables_than_the_cursor_has_columns_is_kept_as_written(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @a int, @b int;\nDECLARE c CURSOR LOCAL FOR SELECT 1 AS x;\nOPEN c;\nFETCH NEXT FROM c INTO @a, @b;")

    fragments = opaque_statements(artefact)
    assert len(fragments) == 1 and "columns" in fragments[0].attrs["reason"]


def test_a_cursor_in_a_variable_is_kept_as_written_with_its_reason(corpus_snapshot):
    artefact, reader = proc(corpus_snapshot, "DECLARE @c CURSOR;\nSET @c = CURSOR FOR SELECT 1 AS x;\nOPEN @c;")
    assert checked(artefact, reader) == []

    fragments = opaque_statements(artefact)
    assert fragments and all("cursor" in f.attrs["reason"].lower() for f in fragments)


def test_update_and_delete_where_current_of_write_the_row_the_cursor_is_on(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @id int;\nDECLARE c CURSOR LOCAL FOR SELECT o.OrderId FROM sales.[Order] AS o FOR UPDATE OF Status;\n"
                       "OPEN c;\nFETCH NEXT FROM c INTO @id;\nUPDATE sales.[Order] SET Status = 2 WHERE CURRENT OF c;\n"
                       "DELETE FROM sales.[Order] WHERE CURRENT OF c;")

    update, delete = of_kind(artefact, "write_current_row")
    assert update.attrs["action"] == "update" and update.attrs["cursor"] == "c" and update.attrs["columns"] == ["Status"]
    assert update.child("value").kind in ("literal", "implicit_convert")
    assert delete.attrs["action"] == "delete" and delete.attrs["target"] == update.attrs["target"]
    assert delete.attrs["target"] == "RetailSample.sales.Order"
    assert [d.kind for d in artefact.body.children_with("statement")].count("opaque_fragment") == 0


def test_a_cursor_opened_and_fetched_after_a_barrier_carries_the_flag(corpus_snapshot):
    artefact = lowered(corpus_snapshot,
                       "DECLARE @id int;\nDECLARE c CURSOR LOCAL FOR SELECT o.OrderId FROM sales.[Order] AS o;\nOPEN c;\n"
                       "EXEC('CLOSE c');\nFETCH NEXT FROM c INTO @id;")

    *_, dynamic, fetch = statements(artefact.body)
    assert fetch.attrs["scope_may_be_stale"] == str(dynamic.span)


def test_the_corpus_cursor_procedures_lower_without_an_opaque_cursor_statement(corpus_snapshot):
    for name in ("sales.uspCloseStaleOrders", "sales.uspReorderReport", "stg.uspPurgeStaging"):
        text = next(SAMPLES.glob(f"{name}.sql")).read_text(encoding="utf-8")
        database, schema, short = "RetailSample", name.split(".")[0], name.split(".")[1]
        artefact, reader = lower_text(corpus_snapshot, text, kind="procedure", schema=schema, name=short,
                                      parameters=_parameters(corpus_snapshot, f"{database}.{name}"))
        assert checked(artefact, reader) == [], name
        cursor_kinds = {"declare_cursor", "open_cursor", "fetch_cursor", "close_cursor", "deallocate_cursor"}
        assert cursor_kinds <= {n.kind for n in artefact.nodes()}, name
        assert not [f for f in opaque_statements(artefact)
                    if any(w in f.attrs["text"].upper() for w in ("CURSOR", "FETCH", "OPEN ", "CLOSE "))], name


def _parameters(snapshot, identity):
    found = [o for o in snapshot.objects if o.identity == identity]
    return list(found[0].fact("parameters").value) if found else []
