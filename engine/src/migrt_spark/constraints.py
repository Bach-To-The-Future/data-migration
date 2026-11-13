"""Constraint enforcement from the catalog (ADR 0003): not null, check, primary key, unique and foreign key, each raising the
error SQL Server raises for it, with its number, severity, state and text.

The checks run before the write, over the rows the statement would produce (`new`) and the rows it leaves alone, so a
statement either passes all of them or changes nothing. They are not atomic with the write: the single-writer assumption.

The order is SQL Server's usual one: null, check, primary key and unique, then foreign key.

Strings are compared as SQL Server compares them under the column's collation: trailing spaces never count, and a
case-insensitive collation folds case. This reproduces the collations the library knows (case-sensitive, case-insensitive
and binary, accent-sensitive); any other fails loudly with `UnsupportedCollation` rather than comparing wrongly."""
from __future__ import annotations

import functools
from decimal import Decimal
from typing import Callable

from pyspark.sql import Column as SparkColumn
from pyspark.sql import DataFrame
from pyspark.sql import functions as F

from .errors import Failure, UnsupportedCollation
from .tables import Column, ForeignKey, TableSpec, Unique

SUPPORTED_TOKENS = {"CI", "CS", "AS", "BIN", "BIN2"}


def is_string(column: Column) -> bool:
    return column.type.upper().startswith(("STRING", "VARCHAR", "CHAR"))


def collation_of(spec: TableSpec, column: Column, default: str) -> str:
    return (column.collation or spec.collation or default).upper()


def normalised(spec: TableSpec, column: Column, expr: SparkColumn, default: str) -> SparkColumn:
    """The value to compare, group or look up by, so that Spark's equality is SQL Server's under the column's collation."""
    if not is_string(column):
        return expr
    collation = collation_of(spec, column, default)
    tokens = set(collation.replace("-", "_").split("_"))
    unsupported = {t for t in tokens if t in ("AI", "KS", "WS", "SC", "VSS")} | (
        {"UTF8"} if "UTF8" in tokens else set())
    if unsupported or not tokens & SUPPORTED_TOKENS:
        raise UnsupportedCollation(
            f"Column {spec.qualified}.{column.name} has collation {collation}, which the support library cannot compare "
            f"exactly yet; no write that compares it is made.")
    value = F.rtrim(expr)
    return F.lower(value) if "CI" in tokens else value


def all_of(conditions: list[SparkColumn]) -> SparkColumn:
    return functools.reduce(lambda a, b: a & b, conditions)


def fmt(value) -> str:
    if value is None:
        return "<NULL>"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex().upper()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def key_text(values: list) -> str:
    return "(" + ", ".join(fmt(v) for v in values) + ")"


# ------------------------------------------------------------------ not null and check

def check_not_null(spec: TableSpec, new: DataFrame, verb: str) -> None:
    required = [c for c in spec.columns if not c.nullable and not c.rowversion]
    if not required:
        return
    counts = new.agg(*[F.sum(F.when(F.col(f"`{c.name}`").isNull(), 1).otherwise(0)).alias(f"n{i}") for i, c in enumerate(required)]
                     ).collect()[0]
    for i, c in enumerate(required):
        if counts[f"n{i}"]:
            raise Failure("not_null_violation", column=c.name, table=f"{spec.database}.{spec.schema}.{spec.name}", verb=verb)


def check_checks(spec: TableSpec, new: DataFrame, verb: str) -> None:
    for ck in spec.checks:
        if not new.where(~F.coalesce(F.expr(ck.expression), F.lit(True))).limit(1).count():
            continue
        raise Failure("check_violation", constraint=ck.name, verb=verb, database=spec.database, table=spec.qualified,
                      column=ck.columns[0] if len(ck.columns) == 1 else None)


# ------------------------------------------------------------------ primary key and unique

def unique_error(spec: TableSpec, u: Unique, values: list) -> Failure:
    if u.index:
        return Failure("unique_index_violation", index=u.name, table=spec.qualified, key=key_text(values))
    return Failure("primary_key_violation" if u.primary else "unique_constraint_violation", constraint=u.name,
                   table=spec.qualified, key=key_text(values))


def _keyed(spec: TableSpec, columns: tuple[str, ...], df: DataFrame, where: str | None, default: str) -> DataFrame:
    if where:
        df = df.where(F.coalesce(F.expr(where), F.lit(False)))
    keys = [normalised(spec, spec.column(c), F.col(f"`{c}`"), default).alias(f"k{i}") for i, c in enumerate(columns)]
    raw = [F.col(f"`{c}`").alias(f"r{i}") for i, c in enumerate(columns)]
    return df.select(*keys, *raw)


def check_unique(spec: TableSpec, new: DataFrame, rest: DataFrame, default: str) -> None:
    """`rest` is the table's rows that the statement leaves as they are."""
    for u in sorted(spec.uniques, key=lambda u: not u.primary):
        n = len(u.columns)
        fresh = _keyed(spec, u.columns, new, u.where, default)
        keys = [f"k{i}" for i in range(n)]
        dup = (fresh.groupBy(*keys).agg(F.count(F.lit(1)).alias("n"), *[F.first(f"r{i}").alias(f"r{i}") for i in range(n)])
               .where("n > 1").limit(1).collect())
        if dup:
            raise unique_error(spec, u, [dup[0][f"r{i}"] for i in range(n)])
        old = _keyed(spec, u.columns, rest, u.where, default).select(*keys)
        cond = all_of([F.col(f"n.k{i}").eqNullSafe(F.col(f"e.k{i}")) for i in range(n)])
        clash = fresh.alias("n").join(old.alias("e"), cond, "left_semi").limit(1).collect()
        if clash:
            raise unique_error(spec, u, [clash[0][f"r{i}"] for i in range(n)])


# ------------------------------------------------------------------ foreign keys

def check_foreign_keys(spec: TableSpec, new: DataFrame, final: DataFrame, verb: str, lookup: Callable[[str], TableSpec],
                       read: Callable[[TableSpec], DataFrame], default: str) -> None:
    """Every new row with a foreign key that is not null must reference a row of the parent. `final` is this table's rows
    after the statement, for a table that references itself."""
    for fk in spec.foreign_keys:
        parent = lookup(fk.references)
        parent_rows = final if parent.target == spec.target else read(parent)
        present = all_of([F.col(f"`{c}`").isNotNull() for c in fk.columns])
        child = new.where(present).select(*[normalised(parent, parent.column(rc), F.col(f"`{c}`"), default).alias(f"k{i}")
                                            for i, (c, rc) in enumerate(zip(fk.columns, fk.ref_columns))])
        keys = parent_rows.select(*[normalised(parent, parent.column(rc), F.col(f"`{rc}`"), default).alias(f"k{i}")
                                    for i, rc in enumerate(fk.ref_columns)])
        cond = all_of([F.col(f"c.k{i}") == F.col(f"p.k{i}") for i in range(len(fk.columns))])
        if child.alias("c").join(keys.alias("p"), cond, "left_anti").limit(1).count():
            raise Failure("foreign_key_violation", constraint=fk.name, kind="FOREIGN KEY", verb=verb,
                          database=parent.database, table=parent.qualified, column=fk.ref_columns[0])


def reference_error(parent: TableSpec, child: TableSpec, fk: ForeignKey, verb: str) -> Failure:
    kind = "SAME TABLE REFERENCE" if child.target == parent.target else "REFERENCE"
    return Failure("foreign_key_violation", constraint=fk.name, kind=kind, verb=verb, database=child.database,
                   table=child.qualified, column=fk.columns[0])
