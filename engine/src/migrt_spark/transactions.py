"""Transactions as compensating restore (spec section 6; ADR 0002 and its amendments).

`BEGIN TRAN` notes nothing; the first write to a table in the transaction notes that table's Delta version, in the
state table too (so a crash can be recovered), and `ROLLBACK` restores those versions. Before restoring anything the helper
checks every table: it refuses if a commit since the start version was not made by this job run, and fails loudly if VACUUM
removed what the version needs. Nothing is restored unless every table can be.

This is not isolation. It rests on the single-writer assumption, which write leases enforce among generated jobs."""
from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

from . import deltaops
from .errors import RestoreRefused, ScopeAborted, VersionVacuumed
from .state import lit

if TYPE_CHECKING:
    from .session import SessionState

NEEDS_MANUAL = "needs manual reconciliation"


def restore_all(spark, versions: dict[str, int], mine: str) -> list[str]:
    """Restore every table whose version moved, or none: returns the tables restored."""
    moved = {t: v for t, v in versions.items() if deltaops.version(spark, t) != v}
    for table, to in moved.items():
        deltaops.check_restorable(spark, table, to, mine)
    done: list[str] = []
    for table, to in moved.items():
        try:
            deltaops.restore(spark, table, to)
        except VersionVacuumed as exc:
            if done:                  # the up-front check could not run here, so this is found late: say what was done
                exc.args = (f"{exc.args[0]} Already restored before it was found: {', '.join(done)}.",)
            raise
        done.append(table)
    return sorted(moved)


class _Real:
    """The one transaction SQL Server has however many times it was begun."""

    def __init__(self, implicit: bool, id: str):
        self.id = id
        self.versions: dict[str, int] = {}
        self.saves: dict[str, dict[str, int]] = {}
        self.created: list[str] = []         # temp tables created inside it, dropped if it is rolled back
        self.depth = 0
        self.implicit = implicit


class Transaction:
    """One BEGIN TRANSACTION, as a block. `commit` and `rollback` end it early; leaving the block commits, and leaving it
    with an exception rolls everything back, as a failed statement under XACT_ABORT ON does. The one exception that leaves
    it open is `ScopeAborted`: an error that ends the scope of a procedure ends neither the transaction the procedure
    opened nor the caller, which goes on inside it and may commit it or roll it back (ADR 0004, amended). With XACT_ABORT
    on the error is not a `ScopeAborted` but the batch's end, and rolls the transaction back."""

    def __init__(self, ss: "SessionState"):
        self.ss, self._real = ss, None

    def __enter__(self) -> "Transaction":
        self._real = self.ss._tx.begin()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._real is None or self.ss._tx.real is not self._real:
            return                                          # already ended by a commit, or a rollback of the whole
        if exc_type is not None and issubclass(exc_type, ScopeAborted):
            self._real = None                               # the scope ended; the transaction is still open in its caller
            return
        if exc_type is not None:
            self.ss._tx.rollback()
        else:
            self.ss._tx.commit()
        self._real = None

    def commit(self) -> None:
        mine, self._real = self._real, None
        self.ss._tx.commit(mine)

    def rollback(self) -> None:
        mine, self._real = self._real, None
        self.ss._tx.rollback(mine)

    def save(self, name: str) -> None:
        self.ss._tx.save(name, self._real)

    def rollback_to(self, name: str) -> None:
        self.ss._tx.rollback_to(name, self._real)


class TxManager:
    def __init__(self, ss: "SessionState"):
        self.ss = ss
        self.real: _Real | None = None
        self._frames = itertools.count()

    @property
    def trancount(self) -> int:
        return self.real.depth if self.real else 0

    def _new(self, implicit: bool) -> _Real:
        return _Real(implicit, f"{self.ss.run}-{next(self._frames)}")       # no randomness: a run's ids are its own sequence

    # ------------------------------------------------------------------ explicit transactions

    def begin(self) -> _Real:
        if self.real is None:
            self.real = self._new(implicit=False)
        self.real.depth += 1
        self.real.implicit = False
        return self.real

    def commit(self, mine: _Real | None = ...) -> None:       # type: ignore[assignment]
        real = self.real if mine is ... else mine
        if real is None or real is not self.real or real.depth == 0:
            raise self.ss.fail("transaction_request_without_begin", request="COMMIT")
        real.depth -= 1
        if real.depth == 0:
            self._finish(real)

    def rollback(self, mine: _Real | None = ...) -> None:     # type: ignore[assignment]
        real = self.real if mine is ... else mine
        if real is None or real is not self.real:
            raise self.ss.fail("transaction_request_without_begin", request="ROLLBACK")
        self._undo(real, dict(real.versions))

    def save(self, name: str, real: _Real | None) -> None:
        if real is None or real is not self.real:
            raise self.ss.fail("transaction_request_without_begin", request="SAVE")
        real.saves[name] = {t: deltaops.version(self.ss.spark, t) for t in real.versions}

    def rollback_to(self, name: str, real: _Real | None) -> None:
        if real is None or real is not self.real or name not in real.saves:
            raise self.ss.fail("savepoint_not_found", name=name)
        saved = real.saves[name]
        self._restore(real, {t: saved.get(t, v) for t, v in real.versions.items()})
        for later in list(real.saves)[list(real.saves).index(name) + 1:]:
            del real.saves[later]

    # ------------------------------------------------------------------ statements that write more than one table

    def begin_statement(self):
        """Statement-level atomicity for a write that fires triggers or cascades: if it fails, all of it is undone."""
        implicit = self.real is None
        if implicit:
            self.real = self._new(implicit=True)
            self.real.depth = 1
        real = self.real
        assert real is not None
        return _Statement(self, real, implicit, {t: deltaops.version(self.ss.spark, t) for t in real.versions})

    # ------------------------------------------------------------------ registering writes

    def register(self, spec) -> None:
        """Note a table's version before the transaction's first write to it."""
        real = self.real
        if real is None or not spec.transactional or spec.target in real.versions:
            return
        v = deltaops.version(self.ss.spark, spec.target)
        real.versions[spec.target] = v
        self.ss.state.put("tx", f"{real.id}|{spec.target}", job=self.ss.job, run=self.ss.run, value=v,
                          heartbeat=self.ss.clock(), status="open")

    def forget(self, target: str) -> None:
        """A table that no longer exists (a temp table dropped at the end of its scope) is not restored."""
        if self.real is not None:
            self.real.versions.pop(target, None)
            for saved in self.real.saves.values():
                saved.pop(target, None)
            if self.real.created.count(target):
                self.real.created.remove(target)
            self.ss.state.delete(f"kind = 'tx' AND key = {lit(self.real.id + '|' + target)}")

    def created(self, target: str) -> None:
        if self.real is not None:
            self.real.created.append(target)

    # ------------------------------------------------------------------ the mechanism

    def _restore(self, real: _Real, to: dict[str, int]) -> None:
        try:
            restore_all(self.ss.spark, to, deltaops.marker(self.ss.job, self.ss.run))
        except (RestoreRefused, VersionVacuumed):
            self.flag(real)
            raise

    def flag(self, real: _Real) -> None:
        self.ss.state.update(f"kind = 'tx' AND key LIKE {lit(real.id + '|%')}", status=lit(NEEDS_MANUAL))

    def _undo(self, real: _Real, to: dict[str, int]) -> None:
        """Roll the whole transaction back: restore, drop what it created, and end it."""
        try:
            self._restore(real, to)
        except (RestoreRefused, VersionVacuumed):
            self.real = None                           # the transaction is over; its entries stay, flagged
            raise
        for target in real.created:
            self.ss._drop_physical(target)
        self._finish(real)

    def _finish(self, real: _Real) -> None:
        self.real = None
        real.depth = 0
        self.ss.state.delete(f"kind = 'tx' AND key LIKE {lit(real.id + '|%')}")

    def doom(self) -> None:
        """An error in a trigger rolls back the whole transaction (SQL Server ends the batch and the transaction)."""
        real = self.real
        if real is not None:
            self._undo(real, dict(real.versions))


class _Statement:
    def __init__(self, tx: TxManager, real: _Real, implicit: bool, saved: dict[str, int]):
        self.tx, self.real, self.implicit, self.saved = tx, real, implicit, saved

    def __enter__(self) -> "_Statement":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        tx, real = self.tx, self.real
        if tx.real is not real:
            return                                               # a trigger's error already ended the transaction
        if exc_type is not None:
            if self.implicit:
                tx._undo(real, dict(real.versions))
            else:
                tx._restore(real, {t: self.saved.get(t, v) for t, v in real.versions.items()})
        elif self.implicit:
            tx._finish(real)
