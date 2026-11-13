"""Differential tests of the support library: SQL Server is the oracle (spec section 7, "The support library's
tests"). A `Duo` is one scratch schema on the sandbox SQL Server and the same-named schema in local Spark, with tables
declared once and created on both sides, and a SQL Server session that stays open for the test so `@@IDENTITY`,
`@@ROWCOUNT` and the like carry over between batches as they do in converted code's session.

The comparison code here shares nothing with the library."""
from __future__ import annotations

import dataclasses
import os
import re
import time
import uuid
from typing import Callable

from migrt.sources.sqlserver import error_mapping
from migrt.sources.sqlserver.connection import Connection, ServerMessage

import migrt_spark as lib


class ShiftedClock:
    """The wall clock plus an offset the test moves, so a lease expires when the test says without waiting for it."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset

    def skip(self, seconds: float) -> None:
        self.offset += seconds


class Oracle(Connection):
    """A SQL Server session that returns a batch's errors instead of raising them, with the rows it did produce."""

    def run(self, sql: str) -> tuple[list[dict], list[ServerMessage]]:
        self._errors = lambda: []                                    # type: ignore[method-assign]
        _, rows = self.capture(sql)
        return rows, [m for m in self._messages if m.severity > 10]


@dataclasses.dataclass(frozen=True)
class C:
    """One column, as the test declares it for both sides."""
    name: str
    tsql_type: str
    nullable: bool = True
    identity: tuple[int, int] | None = None
    default: str | None = None
    collation: str | None = None

    @property
    def rowversion(self) -> bool:
        return self.tsql_type == "rowversion"

    def spark_type(self) -> str:
        base = self.tsql_type.lower()
        if base.startswith(("varchar", "nvarchar", "char", "nchar")):
            return "STRING"
        if base.startswith("decimal"):
            return base.upper()
        return {"int": "INT", "bigint": "BIGINT", "smallint": "SMALLINT", "bit": "BOOLEAN",
                "rowversion": "BINARY"}[base]

    def tsql(self) -> str:
        text = f"[{self.name}] {self.tsql_type}"
        if self.collation:
            text += f" COLLATE {self.collation}"
        if self.identity:
            text += f" IDENTITY({self.identity[0]}, {self.identity[1]})"
        if self.default is not None:
            text += f" DEFAULT ({self.default})"
        return text + ("" if self.nullable or self.rowversion else " NOT NULL")

    def library(self) -> lib.Column:
        return lib.Column(self.name, self.spark_type(), nullable=self.nullable, identity=self.identity,
                          rowversion=self.rowversion, default=self.default, collation=self.collation)


@dataclasses.dataclass(frozen=True)
class FK:
    name: str
    columns: tuple[str, ...]
    references: str                    # the referenced table's name in this schema
    ref_columns: tuple[str, ...]
    on_delete: str = "NO ACTION"
    on_update: str = "NO ACTION"


class Duo:
    def __init__(self, spark, oracle: Oracle, database: str, state_table: bool = True):
        self.spark = spark
        self.oracle = oracle
        self.database = database
        self.schema = "s" + uuid.uuid4().hex[:8]
        self.specs: dict[str, lib.TableSpec] = {}
        self._sessions: list[lib.SessionState] = []
        self.clock = ShiftedClock()
        oracle.run(f"CREATE SCHEMA [{self.schema}]")
        spark.sql(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
        self.state_table = f"{self.schema}.migrt_state"
        if state_table:
            lib.create_state_table(spark, self.state_table)

    # ------------------------------------------------------------------ SQL Server

    def run(self, sql: str) -> tuple[list[dict], list[ServerMessage]]:
        return self.oracle.run(sql.replace("{s}", f"[{self.schema}]"))

    def tsql(self, sql: str) -> list[dict]:
        """Run on SQL Server; any error fails the test."""
        rows, errors = self.run(sql)
        assert not errors, errors
        return rows

    def tsql_error(self, sql: str) -> ServerMessage:
        rows, errors = self.run(sql)
        assert errors, "SQL Server did not raise an error"
        return errors[0]

    # ------------------------------------------------------------------ tables on both sides

    def table(self, name: str, columns: list[C], *, primary_key: tuple[str, ...] | None = None, pk_name: str | None = None,
              uniques: tuple[lib.Unique, ...] = (), checks: tuple[lib.Check, ...] = (), fks: tuple[FK, ...] = (),
              triggers: tuple[lib.Trigger, ...] = ()) -> lib.TableSpec:
        """Create the table on both sides and return the library's description of it."""
        parts = [c.tsql() for c in columns]
        constraints: list[lib.Unique] = list(uniques)
        if primary_key:
            pk_name = pk_name or f"PK_{name}"
            parts.append(f"CONSTRAINT [{pk_name}] PRIMARY KEY ({', '.join(f'[{c}]' for c in primary_key)})")
            constraints.insert(0, lib.Unique(pk_name, primary_key, primary=True))
        for u in uniques:
            if not u.index:
                parts.append(f"CONSTRAINT [{u.name}] UNIQUE ({', '.join(f'[{c}]' for c in u.columns)})")
        for ck in checks:
            parts.append(f"CONSTRAINT [{ck.name}] CHECK ({ck.expression})")
        for fk in fks:
            parts.append(f"CONSTRAINT [{fk.name}] FOREIGN KEY ({', '.join(f'[{c}]' for c in fk.columns)}) REFERENCES "
                         f"[{self.schema}].[{fk.references}] ({', '.join(f'[{c}]' for c in fk.ref_columns)}) "
                         f"ON DELETE {fk.on_delete} ON UPDATE {fk.on_update}")
        self.tsql(f"CREATE TABLE {{s}}.[{name}] ({', '.join(parts)})")
        for u in uniques:
            if u.index:
                where = f" WHERE {u.where}" if u.where else ""
                self.tsql(f"CREATE UNIQUE INDEX [{u.name}] ON {{s}}.[{name}] ({', '.join(f'[{c}]' for c in u.columns)}){where}")
        self.spark.sql(f"CREATE TABLE {self.schema}.{name} ({', '.join(f'`{c.name}` {c.spark_type()}' for c in columns)}) USING DELTA")
        spec = lib.TableSpec(
            target=f"{self.schema}.{name}", database=self.database, schema=self.schema, name=name,
            columns=tuple(c.library() for c in columns), uniques=tuple(constraints), checks=checks,
            foreign_keys=tuple(lib.ForeignKey(fk.name, fk.columns, f"{self.schema}.{fk.references}", fk.ref_columns,
                                              on_delete=fk.on_delete, on_update=fk.on_update) for fk in fks),
            triggers=triggers)
        self.specs[name] = spec
        return spec

    def with_triggers(self, name: str, *triggers: lib.Trigger) -> lib.TableSpec:
        self.specs[name] = dataclasses.replace(self.specs[name], triggers=triggers)
        return self.specs[name]

    def target(self, name: str) -> str:
        return f"{self.schema}.{name}"

    def session(self, *, job: str = "job", run: str | None = None, spark=None, config: lib.Config | None = None,
                settings=None, errors=True, **more) -> lib.SessionState:
        """A session state over this Duo's tables with the clock and the state table the tests use. Each call gets its own
        Spark session, as each job run has its own in production."""
        ss = lib.SessionState.open(
            (spark or self.spark).newSession(), job=job, run=run or uuid.uuid4().hex[:8], state_table=self.state_table,
            tables=list(self.specs.values()), settings=settings,
            errors=error_mapping.load() if errors is True else (errors or None),
            config=config or lib.Config(scratch_schema=self.schema, database=self.database, clock=self.clock), **more)
        self._sessions.append(ss)
        return ss

    def close(self) -> None:
        for ss in self._sessions:
            ss.abandon()

    # ------------------------------------------------------------------ comparison

    def sql_rows(self, name: str, columns: list[str], order: str) -> list[tuple]:
        cols = ", ".join(f"[{c}]" for c in columns)
        return [tuple(r.values()) for r in self.tsql(f"SELECT {cols} FROM {{s}}.[{name}] ORDER BY {order}")]

    def delta_rows(self, name: str, columns: list[str], order: str) -> list[tuple]:
        rows = self.spark.table(f"{self.schema}.{name}").select(*columns).collect()
        keys = [o.strip().split()[0].strip("[]") for o in order.split(",")]
        index = {c: i for i, c in enumerate(columns)}
        return [tuple(r) for r in sorted(rows, key=lambda r: tuple((r[index[k]] is not None, r[index[k]]) for k in keys))]

    def assert_same(self, name: str, columns: list[str], order: str | None = None) -> list[tuple]:
        order = order or columns[0]
        expected, actual = self.sql_rows(name, columns, order), self.delta_rows(name, columns, order)
        assert actual == expected, f"{name}: SQL Server has {expected}, the target has {actual}"
        return expected


def error_of(fn: Callable[[], object]) -> lib.TsqlError:
    try:
        fn()
    except lib.TsqlError as exc:
        return exc
    raise AssertionError("the call did not raise a T-SQL error")


def assert_same_error(duo: Duo, tsql: str, call: Callable[[], object]) -> lib.TsqlError:
    """The call raises the error SQL Server raises for the batch: number, severity, state and message."""
    expected = duo.tsql_error(tsql)
    actual = error_of(call)
    assert (actual.number, actual.severity, actual.state, actual.message) == \
        (expected.number, expected.severity, expected.state, expected.text)
    return actual


def sql_server_password() -> str:
    return os.environ["MSSQL_SA_PASSWORD"]


def scrub(text: str) -> str:
    return re.sub(r"\s+", " ", text)
