"""The write wrappers (spec section 6; ADR 0003): constraints, foreign-key actions, triggers, row counts and the
columns an UPDATE named, each compared with SQL Server running the same statements on the same tables."""
import pytest
from pyspark.sql import functions as F

import migrt_spark as lib
from support_harness import C, FK, assert_same_error, error_of


def parent_child(duo, *, child_action="CASCADE"):
    p = duo.table("P", [C("Id", "int", nullable=False, identity=(10, 2)), C("Name", "varchar(10)", nullable=False),
                        C("Qty", "int")],
                  primary_key=("Id",), uniques=(lib.Unique("UQ_P", ("Name",)),),
                  checks=(lib.Check("CK_P", "Qty > 0", ("Qty",)),))
    c = duo.table("C", [C("Id", "int", nullable=False, identity=(1, 1)), C("PId", "int"), C("Tag", "varchar(5)")],
                  primary_key=("Id",), uniques=(lib.Unique("UX_C", ("Tag",), index=True, where="Tag IS NOT NULL"),),
                  fks=(FK("FK_C", ("PId",), "P", ("Id",), on_delete=child_action),))
    return p, c


def seed(duo, ss):
    duo.tsql("INSERT {s}.P(Name, Qty) VALUES ('a', 1), ('b', 2)")
    ss.insert_values(duo.target("P"), [{"Name": "a", "Qty": 1}, {"Name": "b", "Qty": 2}])
    duo.tsql("INSERT {s}.C(PId, Tag) VALUES (10, 'x'), (12, NULL)")
    ss.insert_values(duo.target("C"), [{"PId": 10, "Tag": "x"}, {"PId": 12, "Tag": None}])


# ---------------------------------------------------------------------- inserts, row counts and identity values

def test_insert_values_assigns_identity_values_and_sets_what_sql_server_sets(duo):
    parent_child(duo)
    ss = duo.session()
    expected = duo.tsql("INSERT {s}.P(Name, Qty) VALUES ('a', 1), ('b', 2); "
                        "SELECT @@ROWCOUNT AS rc, SCOPE_IDENTITY() AS si, @@IDENTITY AS ident, @@ERROR AS err")[0]
    ss.insert_values(duo.target("P"), [{"Name": "a", "Qty": 1}, {"Name": "b", "Qty": 2}])
    assert (ss.rowcount, ss.scope_identity(), ss.identity, ss.at_error) == \
        (expected["rc"], int(expected["si"]), int(expected["ident"]), expected["err"])
    assert duo.assert_same("P", ["Id", "Name", "Qty"]) == [(10, "a", 1), (12, "b", 2)]


def test_insert_from_a_dataframe_takes_identity_values_in_row_order(duo):
    parent_child(duo)
    ss = duo.session()
    duo.tsql("INSERT {s}.P(Name, Qty) SELECT n, q FROM (VALUES ('c', 3), ('a', 1), ('b', 2)) AS v(n, q)")
    df = duo.spark.createDataFrame([("c", 3), ("a", 1), ("b", 2)], "Name string, Qty int").coalesce(1)
    ss.insert(duo.target("P"), df)
    assert ss.rowcount == 3
    assert duo.assert_same("P", ["Id", "Name", "Qty"]) == [(10, "c", 3), (12, "a", 1), (14, "b", 2)]


def test_an_insert_of_no_rows_leaves_the_last_identity_value_alone(duo):
    parent_child(duo)
    ss = duo.session()
    duo.tsql("INSERT {s}.P(Name, Qty) VALUES ('a', 1)")
    ss.insert_values(duo.target("P"), [{"Name": "a", "Qty": 1}])
    expected = duo.tsql("INSERT {s}.P(Name, Qty) SELECT 'z', 1 WHERE 1 = 0; "
                        "SELECT @@ROWCOUNT AS rc, SCOPE_IDENTITY() AS si")[0]
    ss.insert(duo.target("P"), duo.spark.createDataFrame([], "Name string, Qty int"))
    assert (ss.rowcount, ss.scope_identity()) == (expected["rc"], int(expected["si"]))


def test_a_column_left_out_of_an_insert_takes_its_default(duo):
    duo.table("D", [C("Id", "int", nullable=False), C("Qty", "int", default="5"), C("Note", "varchar(5)")])
    ss = duo.session()
    duo.tsql("INSERT {s}.D(Id) VALUES (1)")
    ss.insert_values(duo.target("D"), [{"Id": 1}])
    assert duo.assert_same("D", ["Id", "Qty", "Note"]) == [(1, 5, None)]


def test_a_value_inserted_explicitly_into_an_identity_column_is_refused_unless_identity_insert_is_on(duo):
    p, _ = parent_child(duo)
    ss = duo.session()
    assert_same_error(duo, "INSERT {s}.P(Id, Name, Qty) VALUES (500, 'q', 1)",
                      lambda: ss.insert_values(duo.target("P"), [{"Id": 500, "Name": "q", "Qty": 1}]))
    duo.tsql("SET IDENTITY_INSERT {s}.P ON; INSERT {s}.P(Id, Name, Qty) VALUES (500, 'q', 1); SET IDENTITY_INSERT {s}.P OFF")
    ss.insert_values(duo.target("P"), [{"Id": 500, "Name": "q", "Qty": 1}], identity_insert=True)
    expected = duo.tsql("INSERT {s}.P(Name, Qty) VALUES ('r', 1); SELECT SCOPE_IDENTITY() AS si")[0]["si"]
    ss.insert_values(duo.target("P"), [{"Name": "r", "Qty": 1}])
    assert ss.scope_identity() == int(expected) == 502


FAILURES = {
    "a duplicate key": ("INSERT {s}.P(Name, Qty) VALUES ('a', 1)", {"Name": "a", "Qty": 1}),
    "a null": ("INSERT {s}.P(Name, Qty) VALUES (NULL, 1)", {"Name": None, "Qty": 1}),
    "a check": ("INSERT {s}.P(Name, Qty) VALUES ('q', 0)", {"Name": "q", "Qty": 0}),
}


@pytest.mark.parametrize("failure", FAILURES)
def test_a_failed_insert_still_consumes_its_identity_values(duo, failure):
    parent_child(duo)
    ss = duo.session()
    seed(duo, ss)
    tsql, row = FAILURES[failure]
    duo.run(tsql)
    error_of(lambda: ss.insert_values(duo.target("P"), [row]))
    expected = duo.tsql("INSERT {s}.P(Name, Qty) VALUES ('n', 1); SELECT SCOPE_IDENTITY() AS si, IDENT_CURRENT('{s}.P') AS ic")[0]
    ss.insert_values(duo.target("P"), [{"Name": "n", "Qty": 1}])
    assert ss.scope_identity() == int(expected["si"])
    assert ss.ident_current(duo.target("P")) == int(expected["ic"])


def test_a_failed_statement_sets_at_error_and_the_next_one_clears_it(duo):
    parent_child(duo)
    ss = duo.session()
    seed(duo, ss)
    rows, errors = duo.run("INSERT {s}.P(Name, Qty) VALUES ('a', 1); SELECT @@ERROR AS err, @@ROWCOUNT AS rc")
    error_of(lambda: ss.insert_values(duo.target("P"), [{"Name": "a", "Qty": 1}]))
    assert (ss.at_error, ss.rowcount) == (rows[0]["err"], rows[0]["rc"]) == (errors[0].number, 0)
    ss.insert_values(duo.target("P"), [{"Name": "n", "Qty": 1}])
    assert ss.at_error == 0 and ss.rowcount == 1


# ---------------------------------------------------------------------- constraints raise SQL Server's errors

INSERT_ERRORS = {
    "not null": ("INSERT {s}.P(Name, Qty) VALUES (NULL, 1)", "P", [{"Name": None, "Qty": 1}]),
    "check": ("INSERT {s}.P(Name, Qty) VALUES ('q', 0)", "P", [{"Name": "q", "Qty": 0}]),
    "unique constraint": ("INSERT {s}.P(Name, Qty) VALUES ('a', 1)", "P", [{"Name": "a", "Qty": 1}]),
    "a case-insensitive duplicate": ("INSERT {s}.P(Name, Qty) VALUES ('A', 1)", "P", [{"Name": "A", "Qty": 1}]),
    "a duplicate that differs by trailing spaces": ("INSERT {s}.P(Name, Qty) VALUES ('a  ', 1)", "P",
                                                    [{"Name": "a  ", "Qty": 1}]),
    "a duplicate inside the statement": ("INSERT {s}.P(Name, Qty) VALUES ('x', 1), ('x', 2)", "P",
                                         [{"Name": "x", "Qty": 1}, {"Name": "x", "Qty": 2}]),
    "primary key": ("INSERT {s}.C(Id, PId) VALUES (1, 10)", "C", [{"Id": 1, "PId": 10}]),
    "unique index": ("INSERT {s}.C(PId, Tag) VALUES (10, 'x')", "C", [{"PId": 10, "Tag": "x"}]),
    "foreign key": ("INSERT {s}.C(PId, Tag) VALUES (999, 'y')", "C", [{"PId": 999, "Tag": "y"}]),
}


@pytest.mark.parametrize("case", INSERT_ERRORS)
def test_an_insert_that_breaks_a_constraint_raises_the_error_sql_server_raises(duo, case):
    parent_child(duo)
    ss = duo.session()
    seed(duo, ss)
    tsql, table, rows = INSERT_ERRORS[case]
    explicit = table == "C" and "Id" in rows[0]
    assert_same_error(duo, tsql if not explicit else
                      "SET IDENTITY_INSERT {s}.C ON; " + tsql, lambda: ss.insert_values(duo.target(table), rows, identity_insert=explicit))


def test_a_null_in_a_unique_column_collides_with_another_null(duo):
    duo.table("N", [C("Id", "int", nullable=False), C("K", "int")], primary_key=("Id",),
              uniques=(lib.Unique("UQ_N", ("K",)),))
    ss = duo.session()
    duo.tsql("INSERT {s}.N VALUES (1, NULL)")
    ss.insert_values(duo.target("N"), [{"Id": 1, "K": None}])
    assert_same_error(duo, "INSERT {s}.N VALUES (2, NULL)", lambda: ss.insert_values(duo.target("N"), [{"Id": 2, "K": None}]))


def test_a_case_sensitive_column_keeps_its_spellings_apart_but_not_trailing_spaces(duo):
    duo.table("CS", [C("Id", "int"), C("Name", "varchar(10)", collation="Latin1_General_CS_AS")],
              uniques=(lib.Unique("UQ_CS", ("Name",)),))
    ss = duo.session()
    duo.tsql("INSERT {s}.CS VALUES (1, 'a'), (2, 'A')")
    ss.insert_values(duo.target("CS"), [{"Id": 1, "Name": "a"}, {"Id": 2, "Name": "A"}])
    assert_same_error(duo, "INSERT {s}.CS VALUES (3, 'a ')", lambda: ss.insert_values(duo.target("CS"), [{"Id": 3, "Name": "a "}]))


def test_a_statement_that_fails_leaves_none_of_its_rows(duo):
    parent_child(duo)
    ss = duo.session()
    seed(duo, ss)
    duo.run("INSERT {s}.P(Name, Qty) VALUES ('new', 1), ('a', 1)")
    error_of(lambda: ss.insert_values(duo.target("P"), [{"Name": "new", "Qty": 1}, {"Name": "a", "Qty": 1}]))
    assert duo.assert_same("P", ["Id", "Name", "Qty"]) == [(10, "a", 1), (12, "b", 2)]


UPDATE_ERRORS = {
    "not null": ("UPDATE {s}.P SET Name = NULL WHERE Id = 10", {"Name": None}, "Id = 10"),
    "check": ("UPDATE {s}.P SET Qty = 0 WHERE Id = 10", {"Qty": 0}, "Id = 10"),
    "unique constraint": ("UPDATE {s}.P SET Name = 'b' WHERE Id = 10", {"Name": "b"}, "Id = 10"),
    "a duplicate inside the statement": ("UPDATE {s}.P SET Name = 'same'", {"Name": "same"}, None),
    "identity column": ("UPDATE {s}.P SET Id = 99 WHERE Id = 10", {"Id": 99}, "Id = 10"),
}


@pytest.mark.parametrize("case", UPDATE_ERRORS)
def test_an_update_that_breaks_a_constraint_raises_the_error_sql_server_raises(duo, case):
    parent_child(duo)
    ss = duo.session()
    seed(duo, ss)
    tsql, assignments, where = UPDATE_ERRORS[case]
    assert_same_error(duo, tsql, lambda: ss.update(duo.target("P"), set=assignments, where=where))


def test_an_update_that_moves_keys_among_themselves_is_judged_on_the_final_state(duo):
    duo.table("K", [C("Id", "int", nullable=False)], primary_key=("Id",))
    ss = duo.session()
    duo.tsql("INSERT {s}.K VALUES (1), (2), (3); UPDATE {s}.K SET Id = Id + 1")
    ss.insert_values(duo.target("K"), [{"Id": 1}, {"Id": 2}, {"Id": 3}])
    ss.update(duo.target("K"), set={"Id": F.expr("Id + 1")})
    assert ss.rowcount == 3
    assert duo.assert_same("K", ["Id"]) == [(2,), (3,), (4,)]


def test_a_child_row_pointing_nowhere_is_refused_on_update_too(duo):
    parent_child(duo)
    ss = duo.session()
    seed(duo, ss)
    assert_same_error(duo, "UPDATE {s}.C SET PId = 999 WHERE Id = 1", lambda: ss.update(duo.target("C"), set={"PId": 999}, where="Id = 1"))


def test_a_parent_that_children_reference_cannot_be_deleted_or_rekeyed_under_no_action(duo):
    parent_child(duo, child_action="NO ACTION")
    ss = duo.session()
    seed(duo, ss)
    assert_same_error(duo, "DELETE {s}.P WHERE Id = 10", lambda: ss.delete(duo.target("P"), where="Id = 10"))
    ss.close()
    duo.table("Q", [C("Id", "int", nullable=False)], primary_key=("Id",))
    duo.table("R", [C("Id", "int", nullable=False), C("QId", "int")], primary_key=("Id",),
              fks=(FK("FK_R", ("QId",), "Q", ("Id",)),))
    ss = duo.session()
    duo.tsql("INSERT {s}.Q VALUES (1); INSERT {s}.R VALUES (1, 1)")
    ss.insert_values(duo.target("Q"), [{"Id": 1}])
    ss.insert_values(duo.target("R"), [{"Id": 1, "QId": 1}])
    assert_same_error(duo, "UPDATE {s}.Q SET Id = 2", lambda: ss.update(duo.target("Q"), set={"Id": 2}))


def test_a_row_of_a_table_that_references_itself_has_its_own_error_text(duo):
    duo.table("S", [C("Id", "int", nullable=False), C("Parent", "int")], primary_key=("Id",),
              fks=(FK("FK_S", ("Parent",), "S", ("Id",)),))
    ss = duo.session()
    duo.tsql("INSERT {s}.S VALUES (1, NULL), (2, 1), (3, 1)")
    ss.insert_values(duo.target("S"), [{"Id": 1, "Parent": None}, {"Id": 2, "Parent": 1}, {"Id": 3, "Parent": 1}])
    assert_same_error(duo, "DELETE {s}.S WHERE Id = 1", lambda: ss.delete(duo.target("S"), where="Id = 1"))
    duo.tsql("DELETE {s}.S WHERE Id IN (1, 2, 3)")
    ss.delete(duo.target("S"), where="Id IN (1, 2, 3)")
    assert ss.rowcount == 3 and duo.assert_same("S", ["Id"]) == []


# ---------------------------------------------------------------------- foreign-key actions

ACTIONS = ["CASCADE", "SET NULL", "SET DEFAULT"]


def parent_with_children(duo, action, *, on_delete=True):
    duo.table("Par", [C("Id", "int", nullable=False)], primary_key=("Id",))
    duo.table("Chi", [C("Id", "int", nullable=False), C("ParId", "int", default="0")], primary_key=("Id",),
              fks=(FK("FK_Chi", ("ParId",), "Par", ("Id",), on_delete=action if on_delete else "NO ACTION",
                      on_update="NO ACTION" if on_delete else action),))
    ss = duo.session()
    duo.tsql("INSERT {s}.Par VALUES (0), (1), (2); INSERT {s}.Chi VALUES (1, 1), (2, 1), (3, 2)")
    ss.insert_values(duo.target("Par"), [{"Id": 0}, {"Id": 1}, {"Id": 2}])
    ss.insert_values(duo.target("Chi"), [{"Id": 1, "ParId": 1}, {"Id": 2, "ParId": 1}, {"Id": 3, "ParId": 2}])
    return ss


@pytest.mark.parametrize("action", ACTIONS)
def test_on_delete_actions_are_reproduced(duo, action):
    ss = parent_with_children(duo, action)
    expected = duo.tsql("DELETE {s}.Par WHERE Id = 1; SELECT @@ROWCOUNT AS rc")[0]["rc"]
    ss.delete(duo.target("Par"), where="Id = 1")
    assert ss.rowcount == expected == 1
    duo.assert_same("Par", ["Id"])
    duo.assert_same("Chi", ["Id", "ParId"])


@pytest.mark.parametrize("action", ACTIONS)
def test_on_update_actions_are_reproduced(duo, action):
    ss = parent_with_children(duo, action, on_delete=False)
    expected = duo.tsql("UPDATE {s}.Par SET Id = 5 WHERE Id = 1; SELECT @@ROWCOUNT AS rc")[0]["rc"]
    ss.update(duo.target("Par"), set={"Id": 5}, where="Id = 1")
    assert ss.rowcount == expected == 1
    duo.assert_same("Par", ["Id"])
    duo.assert_same("Chi", ["Id", "ParId"])


def test_a_cascade_reaches_the_grandchildren(duo):
    duo.table("A", [C("Id", "int", nullable=False)], primary_key=("Id",))
    duo.table("B", [C("Id", "int", nullable=False), C("AId", "int")], primary_key=("Id",),
              fks=(FK("FK_B", ("AId",), "A", ("Id",), on_delete="CASCADE"),))
    duo.table("G", [C("Id", "int", nullable=False), C("BId", "int")], primary_key=("Id",),
              fks=(FK("FK_G", ("BId",), "B", ("Id",), on_delete="CASCADE"),))
    ss = duo.session()
    duo.tsql("INSERT {s}.A VALUES (1), (2); INSERT {s}.B VALUES (1, 1), (2, 2); INSERT {s}.G VALUES (1, 1), (2, 1), (3, 2)")
    ss.insert_values(duo.target("A"), [{"Id": 1}, {"Id": 2}])
    ss.insert_values(duo.target("B"), [{"Id": 1, "AId": 1}, {"Id": 2, "AId": 2}])
    ss.insert_values(duo.target("G"), [{"Id": 1, "BId": 1}, {"Id": 2, "BId": 1}, {"Id": 3, "BId": 2}])
    duo.tsql("DELETE {s}.A WHERE Id = 1")
    ss.delete(duo.target("A"), where="Id = 1")
    for name in ("A", "B", "G"):
        duo.assert_same(name, ["Id"])
    assert ss.rowcount == 1


def test_a_cascade_into_a_child_with_a_trigger_that_fails_undoes_the_parent_delete_too(duo):
    def failing(ss_, inserted, deleted):
        raise lib.TsqlError(50000, 16, 1, "no deletes here")

    duo.table("Par", [C("Id", "int", nullable=False)], primary_key=("Id",))
    duo.table("Chi", [C("Id", "int", nullable=False), C("ParId", "int")], primary_key=("Id",),
              fks=(FK("FK_Chi", ("ParId",), "Par", ("Id",), on_delete="CASCADE"),))
    duo.tsql("CREATE TRIGGER {s}.trChi ON {s}.Chi AFTER DELETE AS THROW 50000, 'no deletes here', 1")
    duo.with_triggers("Chi", lib.Trigger("trChi", "after", frozenset({"delete"}), failing))
    ss = duo.session()
    duo.tsql("INSERT {s}.Par VALUES (1); INSERT {s}.Chi VALUES (1, 1)")
    ss.insert_values(duo.target("Par"), [{"Id": 1}])
    ss.insert_values(duo.target("Chi"), [{"Id": 1, "ParId": 1}])
    assert_same_error(duo, "DELETE {s}.Par WHERE Id = 1", lambda: ss.delete(duo.target("Par"), where="Id = 1"))
    assert duo.assert_same("Par", ["Id"]) == [(1,)] and duo.assert_same("Chi", ["Id"]) == [(1,)]


# ---------------------------------------------------------------------- row counts and the columns an UPDATE named

def test_update_and_delete_set_the_row_count_and_an_update_that_matches_nothing_sets_zero(duo):
    parent_child(duo)
    ss = duo.session()
    seed(duo, ss)
    for tsql, call in [
        ("UPDATE {s}.P SET Qty = 7 WHERE Qty < 99", lambda: ss.update(duo.target("P"), set={"Qty": 7}, where="Qty < 99")),
        ("UPDATE {s}.P SET Qty = 7 WHERE Id = 999", lambda: ss.update(duo.target("P"), set={"Qty": 7}, where="Id = 999")),
        ("DELETE {s}.C WHERE Id = 1", lambda: ss.delete(duo.target("C"), where="Id = 1")),
        ("DELETE {s}.C WHERE Id = 999", lambda: ss.delete(duo.target("C"), where="Id = 999")),
    ]:
        expected = duo.tsql(tsql + "; SELECT @@ROWCOUNT AS rc")[0]["rc"]
        call()
        assert ss.rowcount == expected
    assert duo.assert_same("P", ["Id", "Name", "Qty"]) == [(10, "a", 7), (12, "b", 7)]


def test_an_update_records_the_columns_it_named_even_when_the_value_does_not_change(duo):
    parent_child(duo)
    ss = duo.session()
    seed(duo, ss)
    ss.update(duo.target("P"), set={"Qty": F.expr("Qty")}, where="Id = 10")
    assert ss.updated("Qty") and ss.updated("qty") and not ss.updated("Name")
    assert ss.updated_columns == {"Qty"}


# ---------------------------------------------------------------------- triggers

def log_table(duo):
    return duo.table("L", [C("Id", "int", nullable=False, identity=(1000, 1)), C("Note", "varchar(60)")], primary_key=("Id",))


def logger(duo, label=""):
    def trigger(ss, inserted, deleted):
        ss.insert_values(duo.target("L"), [{"Note": f"{label}rc={ss.rowcount} i={inserted.count()} d={deleted.count()}"}])
    return trigger


TRIGGER_BODY = ("INSERT {s}.L(Note) SELECT CONCAT('<label>rc=', @@ROWCOUNT, ' i=', (SELECT COUNT(*) FROM inserted), "
                "' d=', (SELECT COUNT(*) FROM deleted))")


def test_an_after_trigger_sees_inserted_and_deleted_rows_and_the_outer_statements_row_count(duo):
    parent_child(duo)
    log_table(duo)
    duo.tsql("CREATE TRIGGER {s}.trP ON {s}.P AFTER INSERT, UPDATE, DELETE AS " + TRIGGER_BODY.replace("<label>", ""))
    duo.with_triggers("P", lib.Trigger("trP", "after", frozenset({"insert", "update", "delete"}), logger(duo)))
    ss = duo.session()
    steps = [
        ("INSERT {s}.P(Name, Qty) VALUES ('a', 1), ('b', 2)",
         lambda: ss.insert_values(duo.target("P"), [{"Name": "a", "Qty": 1}, {"Name": "b", "Qty": 2}])),
        ("UPDATE {s}.P SET Qty = 5", lambda: ss.update(duo.target("P"), set={"Qty": 5})),
        ("DELETE {s}.P WHERE Name = 'a'", lambda: ss.delete(duo.target("P"), where="Name = 'a'")),
    ]
    for tsql, call in steps:
        expected = duo.tsql(tsql + "; SELECT @@ROWCOUNT AS rc, SCOPE_IDENTITY() AS si, @@IDENTITY AS ident")[0]
        call()
        assert ss.rowcount == expected["rc"]
        assert ss.scope_identity() == (None if expected["si"] is None else int(expected["si"]))
        assert ss.identity == int(expected["ident"])
    assert duo.assert_same("L", ["Id", "Note"]) == [(1000, "rc=2 i=2 d=0"), (1001, "rc=2 i=2 d=2"), (1002, "rc=1 i=0 d=1")]
    duo.assert_same("P", ["Id", "Name", "Qty"])


def test_an_instead_of_trigger_runs_in_place_of_the_write(duo):
    duo.table("I", [C("Id", "int"), C("V", "int")])
    log_table(duo)
    duo.tsql("CREATE TRIGGER {s}.trI ON {s}.I INSTEAD OF INSERT AS " + TRIGGER_BODY.replace("<label>", "instead "))
    duo.with_triggers("I", lib.Trigger("trI", "instead_of", frozenset({"insert"}), logger(duo, "instead ")))
    ss = duo.session()
    expected = duo.tsql("INSERT {s}.I VALUES (1, 1), (2, 2); SELECT @@ROWCOUNT AS rc")[0]["rc"]
    ss.insert_values(duo.target("I"), [{"Id": 1, "V": 1}, {"Id": 2, "V": 2}])
    assert ss.rowcount == expected == 2
    assert duo.assert_same("I", ["Id", "V"]) == []
    duo.assert_same("L", ["Id", "Note"])


def test_several_triggers_on_one_table_fire_in_the_order_the_catalog_records(duo):
    duo.table("T", [C("Id", "int")])
    log_table(duo)
    duo.tsql("CREATE TRIGGER {s}.trFirst ON {s}.T AFTER INSERT AS INSERT {s}.L(Note) VALUES ('first')")
    duo.tsql("CREATE TRIGGER {s}.trSecond ON {s}.T AFTER INSERT AS INSERT {s}.L(Note) VALUES ('second')")
    duo.tsql("EXEC sp_settriggerorder @triggername = N'{s}.trFirst', @order = N'First', @stmttype = N'INSERT'".replace("{s}", duo.schema))
    note = lambda text: (lambda ss, i, d: ss.insert_values(duo.target("L"), [{"Note": text}]))   # noqa: E731
    duo.with_triggers("T", lib.Trigger("trFirst", "after", frozenset({"insert"}), note("first")),
                      lib.Trigger("trSecond", "after", frozenset({"insert"}), note("second")))
    ss = duo.session()
    duo.tsql("INSERT {s}.T VALUES (1)")
    ss.insert_values(duo.target("T"), [{"Id": 1}])
    assert [n for _, n in duo.assert_same("L", ["Id", "Note"])] == ["first", "second"]


def test_a_disabled_trigger_does_not_fire(duo):
    duo.table("T", [C("Id", "int")])
    log_table(duo)
    duo.with_triggers("T", lib.Trigger("trT", "after", frozenset({"insert"}), logger(duo), enabled=False))
    ss = duo.session()
    ss.insert_values(duo.target("T"), [{"Id": 1}])
    assert duo.delta_rows("L", ["Id"], "Id") == []


def test_if_update_is_true_for_every_column_of_an_insert_and_for_the_named_columns_of_an_update(duo):
    parent_child(duo)
    log_table(duo)
    duo.tsql("CREATE TRIGGER {s}.trP ON {s}.P AFTER INSERT, UPDATE AS INSERT {s}.L(Note) "
             "SELECT CONCAT('qty=', IIF(UPDATE(Qty), 1, 0), ' name=', IIF(UPDATE(Name), 1, 0))")

    def trigger(ss, inserted, deleted):
        ss.insert_values(duo.target("L"), [{"Note": f"qty={int(ss.updated('Qty'))} name={int(ss.updated('Name'))}"}])

    duo.with_triggers("P", lib.Trigger("trP", "after", frozenset({"insert", "update"}), trigger))
    ss = duo.session()
    duo.tsql("INSERT {s}.P(Name, Qty) VALUES ('a', 1); UPDATE {s}.P SET Qty = 2")
    ss.insert_values(duo.target("P"), [{"Name": "a", "Qty": 1}])
    ss.update(duo.target("P"), set={"Qty": 2})
    assert [n for _, n in duo.assert_same("L", ["Id", "Note"])] == ["qty=1 name=1", "qty=1 name=0"]


def test_an_error_in_a_trigger_undoes_the_statement_and_the_whole_open_transaction(duo):
    duo.table("K", [C("Id", "int")])
    duo.table("F", [C("Id", "int")])
    duo.tsql("CREATE TRIGGER {s}.trF ON {s}.F AFTER INSERT AS INSERT {s}.K VALUES (CAST('x' AS int))")

    def failing(ss, inserted, deleted):
        raise lib.TsqlError(245, 16, 1, "Conversion failed when converting the varchar value 'x' to data type int.")

    duo.with_triggers("F", lib.Trigger("trF", "after", frozenset({"insert"}), failing))
    ss = duo.session()
    duo.run("BEGIN TRAN; INSERT {s}.K VALUES (1); INSERT {s}.F VALUES (1)")
    sql_state = duo.tsql("SELECT @@TRANCOUNT AS tc, (SELECT COUNT(*) FROM {s}.K) AS k, (SELECT COUNT(*) FROM {s}.F) AS f")[0]
    with pytest.raises(lib.TsqlError):
        with ss.transaction():
            ss.insert_values(duo.target("K"), [{"Id": 1}])
            ss.insert_values(duo.target("F"), [{"Id": 1}])
    assert ss.trancount == sql_state["tc"] == 0
    assert (len(duo.delta_rows("K", ["Id"], "Id")), len(duo.delta_rows("F", ["Id"], "Id"))) == (sql_state["k"], sql_state["f"])


def ping_pong(duo, levels):
    """Two tables whose AFTER INSERT triggers insert into each other, `levels` times in all."""
    duo.table("R1", [C("Id", "int"), C("N", "int")])
    duo.table("R2", [C("Id", "int"), C("N", "int")])
    duo.tsql(f"CREATE TRIGGER {{s}}.trR1 ON {{s}}.R1 AFTER INSERT AS INSERT {{s}}.R2 SELECT Id, N + 1 FROM inserted WHERE N < {levels}")
    duo.tsql(f"CREATE TRIGGER {{s}}.trR2 ON {{s}}.R2 AFTER INSERT AS INSERT {{s}}.R1 SELECT Id, N + 1 FROM inserted WHERE N < {levels}")

    def forward(target):
        return lambda ss, inserted, deleted: ss.insert(duo.target(target), inserted.where(f"N < {levels}").selectExpr("Id", "N + 1 AS N"))

    duo.with_triggers("R1", lib.Trigger("trR1", "after", frozenset({"insert"}), forward("R2")))
    duo.with_triggers("R2", lib.Trigger("trR2", "after", frozenset({"insert"}), forward("R1")))


@pytest.mark.parametrize("levels", [31, 32])
def test_triggers_that_fire_each_other_stop_at_sql_servers_nesting_limit_and_undo_everything(duo, levels):
    ping_pong(duo, levels)
    ss = duo.session()
    rows, errors = duo.run("INSERT {s}.R1 VALUES (1, 0)")
    if errors:
        assert_same = (errors[0].number, errors[0].severity, errors[0].state, errors[0].text)
        actual = error_of(lambda: ss.insert_values(duo.target("R1"), [{"Id": 1, "N": 0}]))
        assert (actual.number, actual.severity, actual.state, actual.message) == assert_same
    else:
        ss.insert_values(duo.target("R1"), [{"Id": 1, "N": 0}])
    assert (len(duo.delta_rows("R1", ["Id"], "Id")), len(duo.delta_rows("R2", ["Id"], "Id"))) ==         (duo.tsql("SELECT COUNT(*) AS n FROM {s}.R1")[0]["n"], duo.tsql("SELECT COUNT(*) AS n FROM {s}.R2")[0]["n"])


def test_a_trigger_that_writes_its_own_table_fires_again_only_when_recursive_triggers_is_on(duo):
    duo.table("Rec", [C("Id", "int"), C("N", "int")])
    duo.tsql("CREATE TRIGGER {s}.trRec ON {s}.Rec AFTER INSERT AS IF @@ROWCOUNT > 0 "
             "INSERT {s}.Rec SELECT Id, N + 1 FROM inserted WHERE N < 3")   # a trigger fires for an empty insert too

    def again(ss, inserted, deleted):
        if ss.rowcount == 0:
            return
        ss.insert(duo.target("Rec"), inserted.where("N < 3").selectExpr("Id", "N + 1 AS N"))

    duo.with_triggers("Rec", lib.Trigger("trRec", "after", frozenset({"insert"}), again))
    ss = duo.session()
    duo.tsql("INSERT {s}.Rec VALUES (1, 0)")
    ss.insert_values(duo.target("Rec"), [{"Id": 1, "N": 0}])
    assert duo.assert_same("Rec", ["Id", "N"], "N") == [(1, 0), (1, 1)]

    duo.tsql(f"ALTER DATABASE [{duo.database}] SET RECURSIVE_TRIGGERS ON")
    try:
        duo.tsql("DELETE {s}.Rec; INSERT {s}.Rec VALUES (1, 0)")
        ss.close()
        again_on = duo.session(config=lib.Config(scratch_schema=duo.schema, database=duo.database, recursive_triggers=True))
        again_on.delete(duo.target("Rec"))
        again_on.insert_values(duo.target("Rec"), [{"Id": 1, "N": 0}])
        assert duo.assert_same("Rec", ["Id", "N"], "N") == [(1, 0), (1, 1), (1, 2), (1, 3)]
    finally:
        duo.tsql(f"ALTER DATABASE [{duo.database}] SET RECURSIVE_TRIGGERS OFF")


def test_an_update_to_a_table_with_a_trigger_and_no_key_is_manual_work_and_says_so(duo):
    duo.table("Keyless", [C("Id", "int"), C("V", "int")])
    log_table(duo)
    duo.with_triggers("Keyless", lib.Trigger("trK", "after", frozenset({"update"}), logger(duo)))
    ss = duo.session()
    ss.insert_values(duo.target("Keyless"), [{"Id": 1, "V": 1}])
    with pytest.raises(lib.ManualWrite, match=duo.target("Keyless")):
        ss.update(duo.target("Keyless"), set={"V": 2})
    assert duo.delta_rows("Keyless", ["Id", "V"], "Id") == [(1, 1)]


def test_a_write_to_a_table_the_catalog_does_not_describe_fails_loudly(duo):
    duo.spark.sql(f"CREATE TABLE {duo.schema}.Unknown (Id INT) USING DELTA")
    ss = duo.session()
    with pytest.raises(lib.UnknownTable, match="Unknown"):
        ss.insert_values(duo.target("Unknown"), [{"Id": 1}])
