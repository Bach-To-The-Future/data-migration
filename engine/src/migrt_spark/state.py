"""The state table (spec section 7): a library-owned Delta table that holds the identity counters, the row-version counter,
write leases, transaction start versions and scratch-table registrations. It is shared by every library version: its schema
is versioned and only ever extended.

One row is a fact about a run or a counter:

  kind          key                         job, run          value               heartbeat  status
  identity      the table                   last writer       the last value      -          -
  rowversion    the database                last writer       the last value      -          -
  lease         the table                   the holder        -                   yes        -
  tx            <transaction>|<table>       the owner         the start version   yes        open / needs manual reconciliation
  scratch       <schema>|<name prefix>      the owner         -                   yes        -
  forced        <time>|<holder run>         the holder        -                   -          detail: who forced it
"""
from __future__ import annotations

from typing import Any

from delta import DeltaTable
from pyspark.sql import Row, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import DoubleType, LongType, StringType, StructField, StructType

from .config import STATE_SCHEMA_VERSION
from .deltaops import retrying

COLUMNS = [("kind", "STRING"), ("key", "STRING"), ("job", "STRING"), ("run", "STRING"), ("value", "BIGINT"),
           ("heartbeat", "TIMESTAMP"), ("status", "STRING"), ("detail", "STRING"), ("updated", "TIMESTAMP")]
LIVENESS_KINDS = ("lease", "tx", "scratch")


def state_table_ddl(name: str) -> str:
    """The state table's definition, for the delivered project and for the validator's fresh sandbox."""
    columns = ",\n  ".join(f"{n} {t}" for n, t in COLUMNS)
    return (f"-- migrt_spark state table, schema version {STATE_SCHEMA_VERSION}. Shared by every library version: "
            f"only ever extended.\nCREATE TABLE IF NOT EXISTS {name} (\n  {columns}\n) USING DELTA")


def create_state_table(spark: SparkSession, name: str) -> None:
    spark.sql(state_table_ddl(name))


def ts(epoch: float) -> str:
    """A SQL expression for a time, exact to the microsecond."""
    return f"timestamp_micros({int(round(epoch * 1_000_000))})"


def lit(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


_SCHEMA = StructType([StructField(n, t, True) for n, t in (
    ("kind", StringType()), ("key", StringType()), ("job", StringType()), ("run", StringType()), ("value", LongType()),
    ("hb", DoubleType()), ("status", StringType()), ("detail", StringType()), ("now", DoubleType()))])


class StateTable:
    def __init__(self, spark: SparkSession, name: str, clock):
        self.spark, self.name, self.clock = spark, name, clock

    def _table(self) -> DeltaTable:
        return DeltaTable.forName(self.spark, self.name)

    def rows(self, where: str = "true") -> list[Row]:
        """Rows matching a predicate, with `hb`, the heartbeat as seconds since the epoch."""
        df = self.spark.table(self.name).where(where)
        return df.selectExpr("*", "cast(heartbeat as double) as hb").collect()

    def get(self, kind: str, key: str) -> Row | None:
        found = self.rows(f"kind = {lit(kind)} AND key = {lit(key)}")
        return found[0] if found else None

    def _source(self, kind: str, key: str, job: str | None, run: str | None, value: int | None, heartbeat: float | None,
                status: str | None, detail: str | None):
        row = (kind, key, job, run, value, heartbeat, status, detail, float(self.clock()))
        return (self.spark.createDataFrame([row], _SCHEMA)
                .select("kind", "key", "job", "run", "value", F.timestamp_seconds("hb").alias("heartbeat"), "status",
                        "detail", F.timestamp_seconds("now").alias("updated")))

    def put(self, kind: str, key: str, *, job: str | None = None, run: str | None = None, value: int | None = None,
            heartbeat: float | None = None, status: str | None = None, detail: str | None = None) -> None:
        """Insert or replace one row."""
        def go():
            src = self._source(kind, key, job, run, value, heartbeat, status, detail)
            (self._table().alias("t").merge(src.alias("s"), "t.kind = s.kind AND t.key = s.key")
             .whenMatchedUpdateAll().whenNotMatchedInsertAll().execute())
        retrying(go)

    def insert_if_absent(self, kind: str, key: str, **fields: Any) -> None:
        def go():
            src = self._source(kind, key, fields.get("job"), fields.get("run"), fields.get("value"),
                               fields.get("heartbeat"), fields.get("status"), fields.get("detail"))
            (self._table().alias("t").merge(src.alias("s"), "t.kind = s.kind AND t.key = s.key")
             .whenNotMatchedInsertAll().execute())
        retrying(go)

    def update(self, where: str, **assignments: Any) -> None:
        """Set columns, each to a SQL expression, on the rows matching `where`."""
        retrying(lambda: self._table().update(where, assignments))

    def delete(self, where: str) -> None:
        retrying(lambda: self._table().delete(where))

    def beat(self, run: str, now: float) -> None:
        kinds = ", ".join(lit(k) for k in LIVENESS_KINDS)
        self.update(f"run = {lit(run)} AND kind IN ({kinds})", heartbeat=ts(now), updated=ts(now))

    # ------------------------------------------------------------------ counters

    def advance(self, kind: str, key: str, count: int, step: int, initial: int, job: str, run: str) -> int:
        """Reserve `count` values: move the counter by `count * step` and return the value it held before. A counter that does
        not exist yet starts from `initial`, the value before the first."""
        for _ in range(20):
            row = self.get(kind, key)
            if row is None:
                self.insert_if_absent(kind, key, job=job, run=run, value=initial + count * step)
                again = self.get(kind, key)
                if again is not None and again["value"] == initial + count * step and again["run"] == run:
                    return initial
                continue
            old = int(row["value"])
            new = old + count * step
            self.update(f"kind = {lit(kind)} AND key = {lit(key)} AND value = {old}", value=str(new), job=lit(job),
                        run=lit(run), updated=ts(self.clock()))
            check = self.get(kind, key)
            if check is not None and int(check["value"]) == new:
                return old
        raise RuntimeError(f"the counter {kind}:{key} kept changing under this writer: is another writer using it?")

    def raise_to(self, kind: str, key: str, value: int, step: int, initial: int, job: str, run: str) -> None:
        """An explicit identity value moves the counter forward if it is beyond it."""
        row = self.get(kind, key)
        if row is None:
            self.insert_if_absent(kind, key, job=job, run=run, value=value)
        elif (value > int(row["value"])) if step > 0 else (value < int(row["value"])):
            self.update(f"kind = {lit(kind)} AND key = {lit(key)}", value=str(value))
