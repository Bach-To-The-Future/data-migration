"""The few things done to Delta tables directly: versions, commit history, restore, and retrying a commit that lost a race."""
from __future__ import annotations

import time
from typing import Callable, TypeVar

from delta import DeltaTable

from .errors import RestoreRefused, VersionVacuumed

T = TypeVar("T")
MARKER_CONF = "spark.databricks.delta.commitInfo.userMetadata"


def marker(job: str, run: str) -> str:
    """What the write wrapper writes into every commit it makes, so a rollback can tell its own commits from others'."""
    return f"migrt:{job}:{run}"


def is_conflict(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}"
    return any(word in text for word in ("ConcurrentAppend", "ConcurrentDelete", "ConcurrentTransaction", "MetadataChanged",
                                         "ProtocolChanged", "ConcurrentWrite", "DELTA_CONCURRENT"))


def retrying(fn: Callable[[], T], attempts: int = 12) -> T:
    """Run a Delta commit again when another committer got in first."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if not is_conflict(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise AssertionError("unreachable")


def version(spark, table: str) -> int:
    return int(DeltaTable.forName(spark, table).history(1).collect()[0]["version"])


# VACUUM only records that it ran: it changes no data, so it is not another writer's commit.
NOT_WRITES = ("VACUUM START", "VACUUM END")


def commits_after(spark, table: str, start: int) -> list[tuple[int, str | None]]:
    rows = (DeltaTable.forName(spark, table).history().where(f"version > {start}")
            .select("version", "userMetadata", "operation").collect())
    return sorted((int(r["version"]), r["userMetadata"]) for r in rows if r["operation"] not in NOT_WRITES)


def check_restorable(spark, table: str, to_version: int, mine: str) -> None:
    """Refuse, before anything is restored, if a commit since `to_version` was not made by `mine` or if VACUUM removed
    files `to_version` needs."""
    foreign = [v for v, who in commits_after(spark, table, to_version) if who != mine]
    if foreign:
        raise RestoreRefused(table, to_version, foreign)
    missing = missing_files(spark, table, to_version)
    if missing:
        raise VersionVacuumed(table, to_version, f"Missing: {', '.join(missing[:3])}.")


def missing_files(spark, table: str, to_version: int) -> list[str]:
    """The files of a table version that no longer exist; empty if all exist or the file system cannot be asked."""
    try:
        jvm, conf = spark._jvm, spark._jsc.hadoopConfiguration()
        files = spark.read.format("delta").option("versionAsOf", to_version).table(table).inputFiles()
        found = []
        for f in files:
            path = jvm.org.apache.hadoop.fs.Path(f)
            if not path.getFileSystem(conf).exists(path):
                found.append(f.rsplit("/", 1)[-1])
        return found
    except (AttributeError, NotImplementedError):
        # No JVM access (Spark Connect): the up-front check cannot run. The restore itself still refuses a missing file, and
        # `restore_all` then names the tables it had already restored; the transaction is flagged either way.
        return []


def restore(spark, table: str, to_version: int) -> None:
    try:
        DeltaTable.forName(spark, table).restoreToVersion(to_version)
    except Exception as exc:
        if "Not all files" in str(exc):
            raise VersionVacuumed(table, to_version) from exc
        raise
