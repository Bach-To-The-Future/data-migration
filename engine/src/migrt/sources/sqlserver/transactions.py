"""Lowering of transactions and savepoints (spec sections 4 and 5, issue #85).

`BEGIN TRANSACTION` is `begin_transaction`, `COMMIT` is `commit_transaction` and `ROLLBACK` is `rollback_transaction`,
`SAVE TRANSACTION name` is `save_point` and `ROLLBACK TRANSACTION name` of a savepoint is `rollback_to_save_point`
(naming it in `save_point`). They are statements, not blocks: a transaction opened in a procedure and not closed there
is still open in its caller, and a rollback or a commit in the caller ends it, so the lowering does not pair a begin
with a commit. Which transactions may be open at a point, and what a rollback undoes, is in the scope
(`core.scopes`); which statement ends a transaction is the program's own.

A name after `ROLLBACK TRANSACTION` is a savepoint of the transaction or the name of its outermost level; one that is
neither, a variable, and the options that change what a transaction is (`DISTRIBUTED`, `WITH MARK`,
`DELAYED_DURABILITY`) are kept as written with the reason: every transaction statement kept as written is a scope
barrier, because it may end the transaction."""
from __future__ import annotations

import re

from ...core.ir import Node
from .expressions import Opaque
from .procedural import Procedural

ISOLATION_LEVELS = {"ReadUncommitted", "ReadCommitted", "RepeatableRead", "Snapshot", "Serializable"}


class Transactions(Procedural):
    def __init__(self, *args):
        super().__init__(*args)
        self.transaction_names: dict[str, str] = {}      # names given to BEGIN TRANSACTION in this body, by folded name
        self.save_point_names: dict[str, str] = {}       # names given to SAVE TRANSACTION in this body

    def transaction_name(self, name, statement) -> str | None:
        """The name a transaction statement gives or refers to: None if it has none; a variable is refused."""
        if name is None:
            return None
        if name.Identifier is None:
            raise Opaque(statement, "a transaction or savepoint named by a variable is not lowered yet: its name is "
                                    "known only when the statement runs")
        return str(name.Value)

    def s_BeginTransactionStatement(self, s) -> list[Node]:
        if s.Distributed:
            raise Opaque(s, "BEGIN DISTRIBUTED TRANSACTION enlists the distributed transaction coordinator: it is not "
                            "lowered")
        if s.MarkDefined:
            raise Opaque(s, "BEGIN TRANSACTION ... WITH MARK writes a mark to the transaction log: it is not lowered")
        name = self.transaction_name(s.Name, s)
        if name is not None:
            self.transaction_names[name.casefold()] = name
        return [Node("begin_transaction", self.span(s), None, {"name": name})]

    def s_CommitTransactionStatement(self, s) -> list[Node]:
        if str(s.DelayedDurabilityOption) == "On":
            raise Opaque(s, "COMMIT ... WITH (DELAYED_DURABILITY = ON) lets the commit return before it is durable: it "
                            "is not lowered")
        return [Node("commit_transaction", self.span(s), None, {"name": self.transaction_name(s.Name, s)})]

    def s_RollbackTransactionStatement(self, s) -> list[Node]:
        name = self.transaction_name(s.Name, s)
        if name is None:
            return [Node("rollback_transaction", self.span(s), None, {"name": None})]
        key = name.casefold()
        if key in self.save_point_names and key in self.transaction_names:
            raise Opaque(s, f"{name} is both a savepoint and the name of a transaction in this body: which one the "
                            "rollback means is decided when it runs")
        if key in self.save_point_names:
            return [Node("rollback_to_save_point", self.span(s), None, {"save_point": self.save_point_names[key]})]
        if key in self.transaction_names:
            return [Node("rollback_transaction", self.span(s), None, {"name": self.transaction_names[key]})]
        raise Opaque(s, f"ROLLBACK TRANSACTION {name}: {name} is neither a savepoint nor a transaction named before this "
                        "point in this body (it may have been set by the caller)")

    def s_SaveTransactionStatement(self, s) -> list[Node]:
        name = self.transaction_name(s.Name, s)
        if name is None:
            raise Opaque(s, "SAVE TRANSACTION without a name")
        self.save_point_names[name.casefold()] = name
        return [Node("save_point", self.span(s), None, {"name": name})]

    def s_SetTransactionIsolationLevelStatement(self, s) -> list[Node]:
        level = str(s.Level)
        if level not in ISOLATION_LEVELS:
            raise Opaque(s, f"the isolation level {level} is not lowered yet")
        return [Node("set_isolation_level", self.span(s), None,
                     {"level": re.sub(r"(?<!^)(?=[A-Z])", " ", level).upper()})]
