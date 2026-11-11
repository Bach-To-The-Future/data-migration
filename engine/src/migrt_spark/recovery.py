"""Recovery on start, write leases and the manual recovery command (spec section 7; ADR 0021 and its amendments).

A run is live while the newest heartbeat among its entries (leases, transaction start versions, scratch registrations) is
fresher than the expiry. Recovery undoes what a run that is no longer live left unfinished: it restores the recorded start
versions, drops the scratch tables and releases the leases. It never touches anything of a live run, unless a forced
recovery names that run, and a forced recovery is recorded in the state table."""
from __future__ import annotations

import dataclasses
import json
import time
from collections import defaultdict

from pyspark.sql import Row, SparkSession

from . import deltaops
from .config import Config
from .errors import LeaseHeld, LeaseWaitTimeout, RecoveryRefused, RestoreRefused, VersionVacuumed
from .state import LIVENESS_KINDS, StateTable, lit
from .transactions import NEEDS_MANUAL, restore_all


@dataclasses.dataclass
class Report:
    recovered: list[str] = dataclasses.field(default_factory=list)           # runs whose leftovers were undone
    restored: list[str] = dataclasses.field(default_factory=list)            # tables restored to their start versions
    dropped: list[str] = dataclasses.field(default_factory=list)             # scratch tables dropped
    skipped_live: list[str] = dataclasses.field(default_factory=list)        # tables under a live lease, left alone
    forced: list[tuple[str, str]] = dataclasses.field(default_factory=list)  # (job, run) whose live lease was forced
    needs_manual: list[str] = dataclasses.field(default_factory=list)        # tables a restore refused to touch


@dataclasses.dataclass(frozen=True)
class ForcedRecovery:
    holder_job: str
    holder_run: str
    by_job: str
    by_run: str
    at: float


def entries_by_run(state: StateTable) -> dict[str, list[Row]]:
    kinds = ", ".join(lit(k) for k in LIVENESS_KINDS)
    grouped: dict[str, list[Row]] = defaultdict(list)
    for row in state.rows(f"kind IN ({kinds})"):
        grouped[row["run"]].append(row)
    return grouped


def newest_heartbeat(rows: list[Row]) -> float:
    return max((r["hb"] for r in rows if r["hb"] is not None), default=0.0)


def is_live(rows: list[Row], now: float, expiry: float) -> bool:
    return newest_heartbeat(rows) > now - expiry


def recover_run(spark: SparkSession, state: StateTable, run: str, rows: list[Row], report: Report) -> None:
    """Undo one run's leftovers. If a restore is refused the entries stay, flagged, and the error is raised: nobody writes
    to those tables until a person has reconciled them."""
    job = rows[0]["job"]
    transactions: dict[str, dict[str, int]] = defaultdict(dict)
    for r in rows:
        if r["kind"] == "tx":
            tx_id, table = r["key"].split("|", 1)
            transactions[tx_id][table] = int(r["value"])
    for tx_id, versions in transactions.items():
        try:
            report.restored += restore_all(spark, versions, deltaops.marker(job, run))
        except (RestoreRefused, VersionVacuumed):
            state.update(f"kind = 'tx' AND key LIKE {lit(tx_id + '|%')}", status=lit(NEEDS_MANUAL))
            report.needs_manual += sorted(versions)
            raise
    for r in rows:
        if r["kind"] == "scratch":
            schema, prefix = r["key"].split("|", 1)
            for t in spark.catalog.listTables(schema):
                if t.name.startswith(prefix):
                    spark.sql(f"DROP TABLE IF EXISTS {schema}.{t.name}")
                    report.dropped.append(f"{schema}.{t.name}")
    state.delete(f"run = {lit(run)} AND kind IN ({', '.join(lit(k) for k in LIVENESS_KINDS)})")
    report.recovered.append(run)


def recover_dead(spark: SparkSession, state: StateTable, config: Config, me: str, report: Report | None = None,
                 now: float | None = None) -> Report:
    """Recover every run, of any job, whose entries have all expired by `now`."""
    report = report or Report()
    now = config.clock() if now is None else now
    for run, rows in sorted(entries_by_run(state).items()):
        if run != me and not is_live(rows, now, config.lease_expiry_seconds):
            recover_run(spark, state, run, rows, report)
    return report


def live_runs_of_job(state: StateTable, config: Config, job: str, me: str, now: float) -> list[tuple[str, list[Row]]]:
    return [(run, rows) for run, rows in sorted(entries_by_run(state).items())
            if run != me and rows[0]["job"] == job and is_live(rows, now, config.lease_expiry_seconds)]


def held_by(rows: list[Row], config: Config, now: float) -> LeaseHeld:
    leases = sorted(r["key"] for r in rows if r["kind"] == "lease")
    return LeaseHeld(leases[0] if leases else None, rows[0]["job"], rows[0]["run"],
                     newest_heartbeat(rows) + config.lease_expiry_seconds, now)


def start_up(spark: SparkSession, state: StateTable, config: Config, job: str, me: str) -> Report:
    """Before any other work: recover what expired. A restarted run of the same job whose earlier run is still live waits
    for it to expire, up to a bounded timeout; with no wait allowed it fails at once, stating when the lease expires."""
    started = config.clock()
    while True:
        now = config.clock()          # one instant for both: a run is either recovered now or waited for, never missed
        report = recover_dead(spark, state, config, me, now=now)
        live = live_runs_of_job(state, config, job, me, now)
        if not live:
            return report
        error = held_by(live[0][1], config, now)
        if config.restart_wait <= 0:
            raise error
        if now - started >= config.restart_wait:
            raise LeaseWaitTimeout(error.table, error.holder_job, error.holder_run, error.expires_at, now, now - started)
        time.sleep(config.poll_seconds)


def recover(spark: SparkSession, state_table: str, *, job: str, run: str, config: Config = Config(),
            force_holder_run: str | None = None) -> Report:
    """The recovery command. By default it touches only runs that are no longer live. Forcing recovery of a live run needs the
    holder's run named; it is recorded in the state table and shown in the next report."""
    state = StateTable(spark, state_table, config.clock)
    now = config.clock()
    grouped = entries_by_run(state)
    live = {r: rows for r, rows in grouped.items() if r != run and is_live(rows, now, config.lease_expiry_seconds)}
    if force_holder_run is not None and force_holder_run not in live:
        raise RecoveryRefused(f"No live lease is held by run {force_holder_run!r}: name the run of the holder to force. "
                              f"Live runs: {sorted(live) or 'none'}.")
    report = Report()
    for other, rows in sorted(grouped.items()):
        if other == run:
            continue
        if other in live and other != force_holder_run:
            report.skipped_live += sorted(r["key"] for r in rows if r["kind"] == "lease")
            continue
        holder_job = rows[0]["job"]
        recover_run(spark, state, other, rows, report)
        if other == force_holder_run:
            report.forced.append((holder_job, other))
            state.put("forced", f"{now}|{other}", job=holder_job, run=other, status="forced",
                      detail=json.dumps({"by_job": job, "by_run": run, "at": now}))
    return report


def forced_recoveries(spark: SparkSession, state_table: str) -> list[ForcedRecovery]:
    """Every forced recovery on record, for the next report."""
    state = StateTable(spark, state_table, time.time)
    out = []
    for r in state.rows("kind = 'forced'"):
        detail = json.loads(r["detail"])
        out.append(ForcedRecovery(r["job"], r["run"], detail["by_job"], detail["by_run"], detail["at"]))
    return sorted(out, key=lambda f: f.at)
