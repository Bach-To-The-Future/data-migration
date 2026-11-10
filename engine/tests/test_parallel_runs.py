"""Sessions working in parallel share one sandbox SQL Server and one Postgres server. Every
scratch database, schema and engagement the tests and the engine create carries a per-run
suffix, so one run never drops or reads another's objects (docs/agents/worktrees.md)."""
import os
import re
import subprocess
import sys
import threading

from migrt import engine
from migrt.core.catalog import Snapshot
from migrt.sources.sqlserver import shapes

from conftest import RUN, SCRATCH, sa_connection, sandbox_server

# Two tests that build, use and drop a scratch database of the suite's, and one that has the
# engine create and drop its own scratch databases.
NODES = ["engine/tests/test_full_extraction.py::test_the_server_and_the_database_levels",
         "engine/tests/test_result_shapes.py::test_procedures_are_recreated_in_the_sandbox_and_described",
         "engine/tests/test_result_shapes.py::test_the_sandbox_holds_nothing_after_an_extraction"]


def leftover(run: str) -> list[str]:
    with sa_connection() as master:
        return [r["name"] for r in master.query(
            "SELECT name FROM sys.databases WHERE name LIKE N'%[_]' + @run OR name LIKE N'migrt[_]sandbox[_]' "
            "+ @run + N'[_]%'", {"run": run})]


def test_the_suites_scratch_databases_carry_the_runs_suffix():
    assert re.fullmatch(r"[0-9a-f]{8}", RUN)
    assert SCRATCH == f"MigrtExtractTest_{RUN}"


def test_two_test_runs_at_once_do_not_interfere():
    runs = ["a1b2c3d4", "e5f6a7b8"]
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    started = [subprocess.Popen([sys.executable, "-m", "pytest", *NODES, "-q", "-p", "no:cacheprovider"], cwd=root,
                                env={**os.environ, "MIGRT_TEST_RUN": run}, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True) for run in runs]
    outputs = [p.communicate(timeout=600)[0] for p in started]

    for p, out in zip(started, outputs):
        assert p.returncode == 0, out[-3000:]
    assert [leftover(run) for run in runs] == [[], []]


def test_two_descriptions_at_once_use_their_own_scratch_databases(engagement):
    snapshot = engine.extract(engagement, sandbox_server(databases=("RetailSample",)))
    sent: dict[str, list[str]] = {"a": [], "b": []}
    results: dict[str, Snapshot] = {}
    failures: list[Exception] = []

    def describe(which: str) -> None:
        address = shapes.SandboxAddress(os.environ.get("MSSQL_HOST", "sqlserver"), 1433, "sa",
                                        os.environ["MSSQL_SA_PASSWORD"], label=f"{RUN}{which}")
        try:
            results[which] = shapes.attach(snapshot, address, statements=sent[which])
        except Exception as failed:      # reported below, from the main thread
            failures.append(failed)

    threads = [threading.Thread(target=describe, args=(which,)) for which in sent]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)

    assert failures == [] and set(results) == {"a", "b"}
    created = {w: re.findall(r"CREATE DATABASE \[(migrt_sandbox_[^\]]+)\]", "\n".join(s)) for w, s in sent.items()}
    assert all(len(names) == 1 for names in created.values()), created
    assert created["a"][0].startswith(f"migrt_sandbox_{RUN}a_") and created["b"][0].startswith(f"migrt_sandbox_{RUN}b_")
    assert created["a"] != created["b"]
    assert results["a"].id == results["b"].id
    assert leftover(RUN + "a") == leftover(RUN + "b") == []
