"""Transactions as compensating restore (spec section 6; ADR 0002 and its amendments), and the counters that a rollback
never resets. Rollback, commit, nesting and savepoints are compared with SQL Server."""
import pytest

import migrt_spark as lib
from support_harness import C, assert_same_error, error_of

COLUMNS = [C("Id", "int", nullable=False), C("V", "int")]


def two_tables(duo):
    duo.table("T1", COLUMNS, primary_key=("Id",))
    duo.table("T2", COLUMNS, primary_key=("Id",))
    ss = duo.session()
    duo.tsql("INSERT {s}.T1 VALUES (1, 10), (2, 20); INSERT {s}.T2 VALUES (1, 10)")
    ss.insert_values(duo.target("T1"), [{"Id": 1, "V": 10}, {"Id": 2, "V": 20}])
    ss.insert_values(duo.target("T2"), [{"Id": 1, "V": 10}])
    return ss


def same_everywhere(duo):
    for name in ("T1", "T2"):
        duo.assert_same(name, ["Id", "V"])


def test_a_rollback_restores_every_table_written_since_the_start(duo):
    ss = two_tables(duo)
    duo.tsql("BEGIN TRAN; INSERT {s}.T1 VALUES (3, 30); UPDATE {s}.T1 SET V = 0; DELETE {s}.T2; ROLLBACK")
    with ss.transaction() as tx:
        ss.insert_values(duo.target("T1"), [{"Id": 3, "V": 30}])
        ss.update(duo.target("T1"), set={"V": 0})
        ss.delete(duo.target("T2"))
        tx.rollback()
    assert ss.trancount == 0
    same_everywhere(duo)


def test_a_commit_keeps_the_writes_and_a_block_that_ends_without_either_commits(duo):
    ss = two_tables(duo)
    duo.tsql("BEGIN TRAN; INSERT {s}.T1 VALUES (3, 30); COMMIT; BEGIN TRAN; DELETE {s}.T2; COMMIT")
    with ss.transaction() as tx:
        ss.insert_values(duo.target("T1"), [{"Id": 3, "V": 30}])
        tx.commit()
    with ss.transaction():
        ss.delete(duo.target("T2"))
    assert ss.trancount == 0
    same_everywhere(duo)


def test_a_block_that_raises_rolls_everything_back_as_xact_abort_on_does(duo):
    ss = two_tables(duo)
    duo.run("SET XACT_ABORT ON; BEGIN TRAN; INSERT {s}.T1 VALUES (3, 30); INSERT {s}.T1 VALUES (1, 99); COMMIT")
    with pytest.raises(lib.TsqlError):
        with ss.transaction():
            ss.insert_values(duo.target("T1"), [{"Id": 3, "V": 30}])
            ss.insert_values(duo.target("T1"), [{"Id": 1, "V": 99}])
    assert ss.trancount == duo.tsql("SELECT @@TRANCOUNT AS tc")[0]["tc"] == 0
    same_everywhere(duo)


def test_trancount_follows_nested_begins_and_a_nested_commit_only_counts_down(duo):
    ss = two_tables(duo)
    seen = []
    with ss.transaction() as outer:
        seen.append(ss.trancount)
        with ss.transaction() as inner:
            seen.append(ss.trancount)
            ss.insert_values(duo.target("T1"), [{"Id": 3, "V": 30}])
            inner.commit()
        seen.append(ss.trancount)
        outer.rollback()
    seen.append(ss.trancount)
    expected = [duo.tsql("BEGIN TRAN; SELECT @@TRANCOUNT AS n")[0]["n"], duo.tsql("BEGIN TRAN; SELECT @@TRANCOUNT AS n")[0]["n"]]
    duo.tsql("INSERT {s}.T1 VALUES (3, 30)")
    expected += [duo.tsql("COMMIT; SELECT @@TRANCOUNT AS n")[0]["n"], duo.tsql("ROLLBACK; SELECT @@TRANCOUNT AS n")[0]["n"]]
    assert seen == expected == [1, 2, 1, 0]
    same_everywhere(duo)


def test_a_rollback_in_a_nested_block_ends_the_whole_transaction_and_the_outer_commit_is_refused(duo):
    ss = two_tables(duo)
    duo.tsql("BEGIN TRAN; BEGIN TRAN; INSERT {s}.T1 VALUES (3, 30); ROLLBACK")
    expected = duo.tsql_error("COMMIT")
    with ss.transaction() as outer:
        with ss.transaction() as inner:
            ss.insert_values(duo.target("T1"), [{"Id": 3, "V": 30}])
            inner.rollback()
        assert ss.trancount == 0
        err = error_of(outer.commit)
    assert (err.number, err.severity, err.state, err.message) == (expected.number, expected.severity, expected.state, expected.text)
    same_everywhere(duo)


def test_commit_after_the_transaction_ended_and_rollback_after_it_ended_are_sql_servers_errors(duo):
    ss = two_tables(duo)
    duo.tsql("BEGIN TRAN; ROLLBACK")
    with ss.transaction() as tx:
        tx.rollback()
        assert_same_error(duo, "COMMIT", tx.commit)
    duo.tsql("BEGIN TRAN; COMMIT")
    with ss.transaction() as tx:
        tx.commit()
        assert_same_error(duo, "ROLLBACK", tx.rollback)


def test_a_savepoint_undoes_only_what_came_after_it(duo):
    ss = two_tables(duo)
    duo.tsql("BEGIN TRAN; INSERT {s}.T1 VALUES (3, 30); SAVE TRAN s1; UPDATE {s}.T1 SET V = 0; DELETE {s}.T2; "
             "ROLLBACK TRAN s1; INSERT {s}.T2 VALUES (2, 20); COMMIT")
    with ss.transaction() as tx:
        ss.insert_values(duo.target("T1"), [{"Id": 3, "V": 30}])
        tx.save("s1")
        ss.update(duo.target("T1"), set={"V": 0})
        ss.delete(duo.target("T2"))
        tx.rollback_to("s1")
        assert ss.trancount == 1
        ss.insert_values(duo.target("T2"), [{"Id": 2, "V": 20}])
    same_everywhere(duo)


def test_rolling_back_to_a_savepoint_that_does_not_exist_is_sql_servers_error(duo):
    ss = two_tables(duo)
    duo.tsql("BEGIN TRAN; SAVE TRAN s1")
    expected = duo.tsql_error("ROLLBACK TRAN nope")
    with ss.transaction() as tx:
        tx.save("s1")
        err = error_of(lambda: tx.rollback_to("nope"))
    assert (err.number, err.severity, err.state, err.message) == (expected.number, expected.severity, expected.state, expected.text)


def test_table_variables_are_left_out_of_the_restore_and_temp_tables_are_not(duo):
    ss = duo.session()
    spec = lambda name: lib.TableSpec(target=name, database=duo.database, schema="dbo", name=name,   # noqa: E731
                                      columns=(lib.Column("Id", "INT"),))
    ss.create_temp_table(spec("#t"))
    ss.create_table_variable(spec("@v"))
    with ss.transaction() as tx:
        ss.insert_values("#t", [{"Id": 1}])
        ss.insert_values("@v", [{"Id": 1}])
        tx.rollback()
    sql = duo.tsql("CREATE TABLE #t (Id int); DECLARE @v TABLE (Id int); BEGIN TRAN; INSERT #t VALUES (1); "
                   "INSERT @v VALUES (1); ROLLBACK; SELECT (SELECT COUNT(*) FROM #t) AS t, (SELECT COUNT(*) FROM @v) AS v")[0]
    assert (ss.table("#t").count(), ss.table("@v").count()) == (sql["t"], sql["v"]) == (0, 1)


def test_the_start_version_of_each_table_written_is_in_the_state_table_until_the_transaction_ends(duo):
    ss = two_tables(duo)
    before = duo.spark.sql(f"DESCRIBE HISTORY {duo.target('T1')}").agg({"version": "max"}).collect()[0][0]
    with ss.transaction() as tx:
        ss.update(duo.target("T1"), set={"V": 0})
        rows = duo.spark.table(duo.state_table).where("kind = 'tx'").collect()
        assert [(r.key.split("|")[1], r.value, r.status) for r in rows] == [(duo.target("T1"), before, "open")]
        tx.commit()
    assert duo.spark.table(duo.state_table).where("kind = 'tx'").count() == 0


def test_a_version_removed_by_vacuum_fails_loudly_and_restores_nothing(duo):
    ss = two_tables(duo)
    with pytest.raises(lib.VersionVacuumed, match=duo.target("T1")):
        with ss.transaction() as tx:
            ss.update(duo.target("T2"), set={"V": 1})
            ss.update(duo.target("T1"), set={"V": 0})
            duo.spark.sql(f"VACUUM {duo.target('T1')} RETAIN 0 HOURS")
            tx.rollback()
    assert duo.delta_rows("T1", ["Id", "V"], "Id") == [(1, 0), (2, 0)]
    assert duo.delta_rows("T2", ["Id", "V"], "Id") == [(1, 1)]                  # not restored either: all or nothing
    flagged = duo.spark.table(duo.state_table).where("kind = 'tx'").collect()
    assert {r.status for r in flagged} == {"needs manual reconciliation"}


def test_when_the_up_front_check_cannot_run_a_late_refusal_names_what_was_already_restored(duo, monkeypatch):
    from migrt_spark import deltaops

    monkeypatch.setattr(deltaops, "missing_files", lambda *a: [])        # no JVM to ask, as under Spark Connect
    ss = two_tables(duo)
    with pytest.raises(lib.VersionVacuumed, match=f"Already restored before it was found: {duo.target('T2')}"):
        with ss.transaction() as tx:
            ss.update(duo.target("T2"), set={"V": 1})
            ss.update(duo.target("T1"), set={"V": 0})
            duo.spark.sql(f"VACUUM {duo.target('T1')} RETAIN 0 HOURS")
            tx.rollback()
    assert duo.delta_rows("T2", ["Id", "V"], "Id") == [(1, 10)]
    assert {r.status for r in duo.spark.table(duo.state_table).where("kind = 'tx'").collect()} == {"needs manual reconciliation"}


def test_a_foreign_commit_in_the_history_refuses_the_restore_and_marks_the_transaction(duo):
    ss = two_tables(duo)
    with pytest.raises(lib.RestoreRefused, match=duo.target("T1")) as refused:
        with ss.transaction() as tx:
            ss.update(duo.target("T1"), set={"V": 0})
            duo.spark.sql(f"INSERT INTO {duo.target('T1')} VALUES (9, 90)")      # a writer that is not this run
            tx.rollback()
    assert refused.value.table == duo.target("T1")
    assert duo.delta_rows("T1", ["Id", "V"], "Id") == [(1, 0), (2, 0), (9, 90)]   # the other writer's rows are not wiped
    flagged = duo.spark.table(duo.state_table).where("kind = 'tx'").collect()
    assert {r.status for r in flagged} == {"needs manual reconciliation"}


def test_a_commit_made_by_this_run_is_not_a_foreign_one(duo):
    ss = two_tables(duo)
    with ss.transaction() as tx:
        for i in range(3):
            ss.update(duo.target("T1"), set={"V": i})
        tx.rollback()
    same_everywhere(duo)


# ---------------------------------------------------------------------- counters

def counters_tables(duo):
    return duo.table("W", [C("Id", "int", nullable=False, identity=(1, 1)), C("V", "int"), C("Ver", "rowversion")],
                     primary_key=("Id",))


def test_a_rollback_never_resets_the_identity_or_row_version_counters(duo):
    counters_tables(duo)
    ss = duo.session()
    start = int.from_bytes(duo.tsql("SELECT @@DBTS AS d")[0]["d"], "big")
    duo.tsql("BEGIN TRAN; INSERT {s}.W(V) VALUES (1), (2); ROLLBACK; INSERT {s}.W(V) VALUES (3)")
    expected = duo.tsql("SELECT SCOPE_IDENTITY() AS si, @@DBTS AS d")[0]
    with ss.transaction() as tx:
        ss.insert_values(duo.target("W"), [{"V": 1}, {"V": 2}])
        tx.rollback()
    ss.insert_values(duo.target("W"), [{"V": 3}])
    assert ss.scope_identity() == int(expected["si"]) == 3
    assert ss.dbts(duo.database) == int.from_bytes(expected["d"], "big") - start
    rows = duo.sql_rows("W", ["Id", "V"], "Id")
    assert duo.delta_rows("W", ["Id", "V"], "Id") == rows == [(3, 3)]


def test_row_versions_are_unique_per_row_and_grow_with_every_insert_and_update(duo):
    counters_tables(duo)
    ss = duo.session()
    ss.insert_values(duo.target("W"), [{"V": 1}, {"V": 2}])
    first = {r.Id: bytes(r.Ver) for r in duo.spark.table(duo.target("W")).collect()}
    ss.update(duo.target("W"), set={"V": 9}, where="Id = 1")
    after = {r.Id: bytes(r.Ver) for r in duo.spark.table(duo.target("W")).collect()}
    assert len({*first.values()}) == 2
    assert after[2] == first[2] and after[1] > first[1] and after[1] > max(first.values())
    assert int.from_bytes(after[1], "big") == ss.dbts(duo.database)
    assert len(after[1]) == 8


def test_the_counters_live_in_the_state_table_so_a_new_run_continues_from_them(duo):
    counters_tables(duo)
    first = duo.session(run="r1")
    first.insert_values(duo.target("W"), [{"V": 1}, {"V": 2}])
    first.close()
    second = duo.session(run="r2")
    second.insert_values(duo.target("W"), [{"V": 3}])
    assert second.scope_identity() == 3
    stored = {(r.kind, r.key): r.value for r in duo.spark.table(duo.state_table).collect() if r.kind in ("identity", "rowversion")}
    assert stored == {("identity", duo.target("W")): 3, ("rowversion", duo.database): 3}


def test_a_table_with_rows_and_no_counter_is_not_guessed_at(duo):
    counters_tables(duo)
    duo.spark.sql(f"INSERT INTO {duo.target('W')} (Id, V) VALUES (7, 1)")      # loaded outside the wrapper
    ss = duo.session()
    with pytest.raises(lib.CounterNotInitialised, match=duo.target("W")):
        ss.insert_values(duo.target("W"), [{"V": 2}])
    assert duo.delta_rows("W", ["Id", "V"], "Id") == [(7, 1)]
