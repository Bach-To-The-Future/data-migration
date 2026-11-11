"""Write leases, heartbeats and recovery on start (spec section 7; ADR 0021 and its amendments). A crash is simulated by
abandoning a session: its heartbeat stops and nothing else is cleaned up, as when a driver is killed."""
import threading
import time

import pytest

import migrt_spark as lib
from support_harness import C

EXPIRY, HEARTBEAT, POLL = 30.0, 1.0, 0.5       # long enough that slow Spark calls never outlast it; tests move the clock


def config(duo, **more):
    base = dict(lease_expiry_seconds=EXPIRY, heartbeat_seconds=HEARTBEAT, poll_seconds=POLL,
                scratch_schema=duo.schema, database=duo.database, clock=duo.clock)
    return lib.Config(**{**base, **more})


def tables(duo):
    duo.table("T1", [C("Id", "int", nullable=False), C("V", "int")], primary_key=("Id",))
    duo.table("T2", [C("Id", "int", nullable=False), C("V", "int")], primary_key=("Id",))
    setup = duo.session(job="setup", run="setup")
    setup.insert_values(duo.target("T1"), [{"Id": 1, "V": 10}, {"Id": 2, "V": 20}])
    setup.insert_values(duo.target("T2"), [{"Id": 1, "V": 10}])
    setup.close()


def leases(duo):
    return {r.key: (r.job, r.run) for r in duo.spark.table(duo.state_table).where("kind = 'lease'").collect()}


def crashed_mid_transaction(duo, job="nightly", run="r1"):
    """A run that wrote T1 inside a transaction and died: the lease, the start version and a scratch table remain."""
    ss = duo.session(job=job, run=run, config=config(duo))
    tx = ss.transaction()
    tx.__enter__()
    ss.update(duo.target("T1"), set={"V": 0})
    ss.create_temp_table(lib.TableSpec(target="#t", database=duo.database, schema="dbo", name="#t",
                                       columns=(lib.Column("Id", "INT"),)))
    ss.abandon()
    return ss


def test_a_lease_is_taken_on_each_table_before_its_first_write_and_released_at_the_end(duo):
    tables(duo)
    ss = duo.session(job="nightly", run="r1", config=config(duo))
    assert leases(duo) == {}
    ss.table(duo.target("T1")).count()                       # reading takes no lease
    assert leases(duo) == {}
    ss.update(duo.target("T1"), set={"V": 1})
    assert leases(duo) == {duo.target("T1"): ("nightly", "r1")}
    ss.close()
    assert leases(duo) == {}


def test_a_lease_covers_the_tables_a_trigger_writes_too(duo):
    tables(duo)
    duo.with_triggers("T1", lib.Trigger("trT1", "after", frozenset({"update"}),
                                        lambda ss, i, d: ss.insert_values(duo.target("T2"), [{"Id": 99, "V": 0}])))
    ss = duo.session(job="nightly", run="r1", config=config(duo))
    ss.update(duo.target("T1"), set={"V": 1}, where="Id = 1")
    assert set(leases(duo)) == {duo.target("T1"), duo.target("T2")}


def test_a_heartbeat_on_a_background_thread_keeps_the_lease_live_while_the_job_is_busy(duo):
    tables(duo)
    expiry = 6.0                                              # real time here: the heartbeat is what is being tested
    ss = duo.session(job="nightly", run="r1", config=config(duo, lease_expiry_seconds=expiry))
    ss.update(duo.target("T1"), set={"V": 1})
    first = duo.spark.table(duo.state_table).where("kind = 'lease'").collect()[0].heartbeat
    time.sleep(expiry + 2 * HEARTBEAT)                        # the job does nothing for longer than the expiry
    last = duo.spark.table(duo.state_table).where("kind = 'lease'").collect()[0].heartbeat
    assert last > first
    other = duo.session(job="other", run="r2", config=config(duo, lease_expiry_seconds=expiry))
    with pytest.raises(lib.LeaseHeld):
        other.update(duo.target("T1"), set={"V": 2})


def test_a_second_writer_of_another_job_fails_at_once_naming_the_table_the_holder_and_the_expiry(duo):
    tables(duo)
    holder = duo.session(job="nightly", run="r1", config=config(duo))
    holder.update(duo.target("T1"), set={"V": 1})
    other = duo.session(job="other", run="r2", config=config(duo))
    started = time.monotonic()
    with pytest.raises(lib.LeaseHeld) as held:
        other.update(duo.target("T1"), set={"V": 2})
    assert time.monotonic() - started < EXPIRY / 2           # it did not wait
    assert (held.value.table, held.value.holder_job, held.value.holder_run) == (duo.target("T1"), "nightly", "r1")
    text = str(held.value)
    assert duo.target("T1") in text and "nightly" in text and "r1" in text and "expires" in text
    assert duo.delta_rows("T1", ["Id", "V"], "Id") == [(1, 1), (2, 1)]


def test_a_lease_is_free_again_once_its_holder_has_ended(duo):
    tables(duo)
    holder = duo.session(job="nightly", run="r1", config=config(duo))
    holder.update(duo.target("T1"), set={"V": 1})
    holder.close()
    other = duo.session(job="other", run="r2", config=config(duo))
    other.update(duo.target("T1"), set={"V": 2})
    assert leases(duo) == {duo.target("T1"): ("other", "r2")}


def test_two_writers_racing_for_one_table_leave_exactly_one_winner(duo):
    tables(duo)
    first = duo.session(job="a", run="ra", config=config(duo))
    second = duo.session(job="b", run="rb", config=config(duo))
    barrier = threading.Barrier(2)
    outcome: dict[str, str] = {}

    def attempt(name, ss):
        barrier.wait()
        try:
            ss.update(duo.target("T1"), set={"V": 5})
            outcome[name] = "won"
        except lib.LeaseHeld:
            outcome[name] = "held"

    threads = [threading.Thread(target=attempt, args=(n, s)) for n, s in (("a", first), ("b", second))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcome.values()) == ["held", "won"]
    assert len(leases(duo)) == 1


def test_an_expired_lease_is_taken_over_after_recovering_the_crashed_runs_transaction_and_scratch_tables(duo):
    tables(duo)
    crashed_mid_transaction(duo)
    assert duo.delta_rows("T1", ["Id", "V"], "Id") == [(1, 0), (2, 0)]
    other = duo.session(job="other", run="r2", config=config(duo))
    with pytest.raises(lib.LeaseHeld):
        other.update(duo.target("T1"), set={"V": 7})
    duo.clock.skip(EXPIRY + 1)
    other.update(duo.target("T1"), set={"V": 7}, where="Id = 1")
    assert duo.delta_rows("T1", ["Id", "V"], "Id") == [(1, 7), (2, 20)]        # the crashed transaction was undone first
    state = duo.spark.table(duo.state_table)
    assert state.where("kind IN ('tx', 'scratch')").count() == 0
    assert leases(duo) == {duo.target("T1"): ("other", "r2")}
    assert not [t for t in duo.spark.catalog.listTables(duo.schema) if "r1" in t.name]


def test_a_recovery_that_a_restore_refuses_stops_only_writers_of_that_table_and_warns_the_run_that_starts(duo):
    tables(duo)
    crashed_mid_transaction(duo, job="nightly", run="r1")
    duo.spark.sql(f"INSERT INTO {duo.target('T1')} VALUES (9, 90)")          # another writer's commit since the start version
    duo.clock.skip(EXPIRY + 1)
    with pytest.warns(UserWarning, match="manual reconciliation"):
        other = duo.session(job="other", run="r2", config=config(duo))      # it starts all the same
    assert other.startup.refused
    other.update(duo.target("T2"), set={"V": 5})                              # a table the crashed run never touched
    with pytest.raises(lib.RestoreRefused, match=duo.target("T1")):
        other.update(duo.target("T1"), set={"V": 7})                          # the table needing a person
    assert {r.status for r in duo.spark.table(duo.state_table).where("kind = 'tx'").collect()} == {"needs manual reconciliation"}


def test_a_start_up_recovery_leaves_alone_everything_under_a_live_lease_and_recovers_what_has_expired(duo):
    tables(duo)
    crashed_mid_transaction(duo)
    other = duo.session(job="other", run="r2", config=config(duo))           # its start-up recovery runs here
    assert duo.delta_rows("T1", ["Id", "V"], "Id") == [(1, 0), (2, 0)]        # the lease is live: nothing was touched
    assert duo.spark.table(duo.state_table).where("kind = 'tx'").count() == 1
    duo.clock.skip(EXPIRY + 1)
    again = duo.session(job="third", run="r3", config=config(duo))           # now it has expired
    assert duo.delta_rows("T1", ["Id", "V"], "Id") == [(1, 10), (2, 20)]
    assert duo.spark.table(duo.state_table).where("kind IN ('tx', 'scratch', 'lease')").count() == 0
    assert other is not again


def test_a_run_of_the_same_job_restarted_within_the_expiry_waits_and_then_proceeds(duo):
    tables(duo)
    crashed_mid_transaction(duo, job="nightly", run="r1")
    result: dict[str, object] = {}

    def restart():
        started = time.monotonic()
        result["ss"] = duo.session(job="nightly", run="r2", config=config(duo))      # waits for r1's lease to expire
        result["waited"] = time.monotonic() - started

    thread = threading.Thread(target=restart)
    thread.start()
    thread.join(timeout=4 * POLL + 4)
    assert thread.is_alive() and "ss" not in result                  # it is waiting: r1's lease is still live
    duo.clock.skip(EXPIRY + 1)                                        # now it has expired
    thread.join(timeout=60)
    assert "ss" in result and result["waited"] >= 2  # type: ignore[operator]
    assert duo.delta_rows("T1", ["Id", "V"], "Id") == [(1, 10), (2, 20)]       # recovered before any other work
    restarted = result["ss"]
    restarted.update(duo.target("T1"), set={"V": 7}, where="Id = 1")           # type: ignore[attr-defined]
    assert leases(duo) == {duo.target("T1"): ("nightly", "r2")}


def test_a_restart_that_may_not_wait_fails_at_once_stating_when_the_lease_expires(duo):
    tables(duo)
    crashed_mid_transaction(duo, job="nightly", run="r1")
    started = time.monotonic()
    with pytest.raises(lib.LeaseHeld) as held:
        duo.session(job="nightly", run="r2", config=config(duo, restart_wait_timeout_seconds=0))
    assert time.monotonic() - started < EXPIRY / 2
    assert "expires" in str(held.value) and held.value.expires_at > time.time()


def test_a_same_job_restart_gives_up_after_its_bounded_timeout_if_the_lease_stays_live(duo):
    tables(duo)
    live = duo.session(job="nightly", run="r1", config=config(duo))
    live.update(duo.target("T1"), set={"V": 1})
    started = time.monotonic()
    with pytest.raises(lib.LeaseWaitTimeout):
        duo.session(job="nightly", run="r2", config=config(duo, restart_wait_timeout_seconds=2.0))
    assert 1.5 < time.monotonic() - started < EXPIRY


def test_the_recovery_command_by_default_leaves_a_live_lease_alone(duo):
    tables(duo)
    live = duo.session(job="nightly", run="r1", config=config(duo))
    live.update(duo.target("T1"), set={"V": 1})
    report = lib.recover(duo.spark.newSession(), duo.state_table, job="ops", run="cmd", config=config(duo))
    assert report.skipped_live == [duo.target("T1")] and report.recovered == []
    assert leases(duo) == {duo.target("T1"): ("nightly", "r1")}


def test_forcing_recovery_of_a_live_lease_needs_the_holders_run_and_is_recorded(duo):
    tables(duo)
    live = duo.session(job="nightly", run="r1", config=config(duo))
    with live.transaction():
        live.update(duo.target("T1"), set={"V": 0})
        with pytest.raises(lib.RecoveryRefused, match="r-wrong"):
            lib.recover(duo.spark.newSession(), duo.state_table, job="ops", run="cmd", config=config(duo),
                        force_holder_run="r-wrong")
        report = lib.recover(duo.spark.newSession(), duo.state_table, job="ops", run="cmd", config=config(duo),
                             force_holder_run="r1")
        assert report.forced == [("nightly", "r1")]
        recorded = lib.forced_recoveries(duo.spark.newSession(), duo.state_table)
        assert [(f.holder_job, f.holder_run, f.by_job, f.by_run) for f in recorded] == [("nightly", "r1", "ops", "cmd")]
        assert duo.delta_rows("T1", ["Id", "V"], "Id") == [(1, 10), (2, 20)]
        assert leases(duo) == {}
        time.sleep(HEARTBEAT * 3)
        with pytest.raises(lib.LeaseLost, match=duo.target("T1")):                # the holder finds out at its next write
            live.update(duo.target("T1"), set={"V": 5})
