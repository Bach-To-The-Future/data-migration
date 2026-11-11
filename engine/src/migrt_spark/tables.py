"""What the write wrappers know about a table, from the catalog (ADR 0003): columns, keys, checks, foreign keys and
triggers. The delivered project holds one description per table the converted code writes."""
from __future__ import annotations

import dataclasses
from typing import Callable


@dataclasses.dataclass(frozen=True)
class Column:
    name: str
    type: str                                  # the Spark type, as the type mapping gives it
    nullable: bool = True
    identity: tuple[int, int] | None = None    # (seed, increment)
    rowversion: bool = False
    default: str | None = None                 # a Spark SQL expression
    collation: str | None = None               # the source collation; None: the table's default


@dataclasses.dataclass(frozen=True)
class Unique:
    """A primary key, a unique constraint or a unique index (a filtered one has `where`, a Spark SQL predicate)."""
    name: str
    columns: tuple[str, ...]
    primary: bool = False
    index: bool = False
    where: str | None = None


@dataclasses.dataclass(frozen=True)
class Check:
    name: str
    expression: str                            # a Spark SQL predicate: a row fails when it is FALSE, not when NULL
    columns: tuple[str, ...] = ()              # the columns it reads: SQL Server names the column when there is one


@dataclasses.dataclass(frozen=True)
class ForeignKey:
    name: str
    columns: tuple[str, ...]
    references: str                            # the referenced table's target name
    ref_columns: tuple[str, ...]
    on_delete: str = "NO ACTION"               # NO ACTION, CASCADE, SET NULL or SET DEFAULT
    on_update: str = "NO ACTION"


@dataclasses.dataclass(frozen=True)
class Trigger:
    """`function(ss, inserted, deleted)`; `timing` is "after" or "instead_of"; `events` a set of "insert", "update",
    "delete". Triggers on a table fire in the order given."""
    name: str
    timing: str
    events: frozenset[str]
    function: Callable
    enabled: bool = True


@dataclasses.dataclass(frozen=True)
class TableSpec:
    target: str                                # the Delta table's name in the target
    database: str                              # the source names, which SQL Server's messages use
    schema: str
    name: str
    columns: tuple[Column, ...]
    uniques: tuple[Unique, ...] = ()
    checks: tuple[Check, ...] = ()
    foreign_keys: tuple[ForeignKey, ...] = ()
    triggers: tuple[Trigger, ...] = ()
    collation: str | None = None               # the database's default collation
    transactional: bool = True                 # False for a table variable

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.columns]

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name.lower() == name.lower():
                return c
        raise KeyError(f"{self.qualified} has no column {name}")

    def has_column(self, name: str) -> bool:
        return any(c.name.lower() == name.lower() for c in self.columns)

    @property
    def identity_column(self) -> Column | None:
        return next((c for c in self.columns if c.identity), None)

    @property
    def rowversion_column(self) -> Column | None:
        return next((c for c in self.columns if c.rowversion), None)

    @property
    def row_identity(self) -> tuple[str, ...] | None:
        """The columns that identify a row: the primary key, else a whole unique constraint on columns that cannot be null."""
        keys = [u for u in self.uniques if not u.where]
        for u in sorted(keys, key=lambda u: not u.primary):
            if u.primary or all(not self.column(c).nullable for c in u.columns):
                return u.columns
        return None

    def triggers_for(self, event: str, timing: str) -> list[Trigger]:
        return [t for t in self.triggers if t.enabled and t.timing == timing and event in t.events]
