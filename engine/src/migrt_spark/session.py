"""Session state (spec sections 6 and 7): what generated code carries in place of the legacy database session, and the one way
it writes.

Every generated function takes it as its first argument. It carries temp tables, the open transaction, `@@ROWCOUNT`,
`@@ERROR`, the last identity value, the error being handled, messages and session values. Its write wrappers enforce the
catalog's constraints and foreign-key actions, fire triggers, assign identity and row-version values from counters in the
state table, take a write lease on every table before the first write to it, register the write with the open transaction,
and record the row count and the columns an UPDATE named.

`set` values of an update: a Spark `Column` is an expression; anything else is a literal value. `where` is a Spark SQL
predicate (a string) or a `Column`."""
from __future__ import annotations

import contextlib
import dataclasses
import functools
import re
import threading
import time
import warnings
from typing import Any, Callable, Iterable, Iterator

from delta import DeltaTable
from pyspark.sql import Column as SparkColumn
from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F

from . import constraints, deltaops, raiserror, recovery
from .config import TRIGGER_NESTING_LIMIT, VERSION_TAG, Config
from .errors import (CounterNotInitialised, ErrorFieldUnknown, ErrorMappingNotLoaded, Failure, LeaseHeld, LeaseLost,
                     LeaseWaitTimeout, ManualWrite, RethrowOutsideHandler, ScopeAborted, TsqlError, UnknownTable,
                     UnsupportedRaise)
from .settings import Settings
from .source_errors import Rendered, SourceErrors
from .target_errors import Classified, SparkFailures
from .state import StateTable, lit
from .tables import ForeignKey, TableSpec
from .transactions import Transaction, TxManager

ORD = "__ord"


@dataclasses.dataclass
class _Scope:
    """A call depth: the temp tables and table variables declared in it, and its last identity value."""
    temps: dict[str, TableSpec] = dataclasses.field(default_factory=dict)       # lower-case name -> spec on the scratch table
    names: dict[str, str] = dataclasses.field(default_factory=dict)             # lower-case name -> the name as written
    owned: list[str] = dataclasses.field(default_factory=list)                  # snapshots to drop when it ends
    scope_identity: int | None = None
    procedure: "str | None | Any" = None        # what ERROR_PROCEDURE() says here: a name, None for a batch, UNKNOWN if not given


UNKNOWN = object()                              # a scope whose generated code never said which procedure it is


@dataclasses.dataclass(frozen=True)
class Message:
    """A message in the session's message stream: PRINT, or RAISERROR at severity 10 or below."""
    number: int
    severity: int
    state: int
    text: str


class _Heartbeat(threading.Thread):
    """Refreshes this run's entries independently of the job's work, so a slow but live job never looks expired."""

    def __init__(self, ss: "SessionState"):
        super().__init__(name=f"migrt-heartbeat-{ss.run}", daemon=True)
        self.ss, self.stop_event, self.error = ss, threading.Event(), None
        self.state = StateTable(ss.spark.newSession(), ss.state.name, ss.clock)

    def run(self) -> None:
        interval = self.ss.config.heartbeat
        while not self.stop_event.wait(interval):
            try:
                held = set(self.ss._held)                        # taken before the read: anything in it is already stored
                self.state.beat(self.ss.run, self.ss.clock())
                present = {r["key"] for r in self.state.rows(f"kind = 'lease' AND run = {lit(self.ss.run)}")}
                self.ss._lost |= held - present
            except Exception as exc:                             # noqa: BLE001 - recorded, and raised at the next write
                self.error = exc

    def stop(self) -> None:
        self.stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=30)


class SessionState:
    def __init__(self, spark: SparkSession, *, job: str, run: str, state_table: str, tables: Iterable[TableSpec] = (),
                 settings: "str | dict | Any | None" = None, config: Config = Config(),
                 errors: "str | dict | SourceErrors | None" = None):
        self.spark, self.job, self.run, self.config = spark, job, run, config
        self.errors: SourceErrors | None = SourceErrors.load(errors) if errors is not None else None
        self.failures = SparkFailures.load()
        self.clock = config.clock
        self.state = StateTable(spark, state_table, config.clock)
        self.settings = Settings.load(settings)
        self.catalog = {t.target.lower(): t for t in tables}
        self.incoming: dict[str, list[tuple[TableSpec, ForeignKey]]] = {}
        for spec in self.catalog.values():
            for fk in spec.foreign_keys:
                self.incoming.setdefault(fk.references.lower(), []).append((spec, fk))
        self.scratch_schema = config.scratch_schema or (state_table.rsplit(".", 1)[0] if "." in state_table else "default")
        self.prefix = f"ms_{VERSION_TAG}_{re.sub(r'[^0-9A-Za-z]', '_', run)}_"
        # what @@ROWCOUNT, @@ERROR, @@IDENTITY and friends hold
        self.rowcount, self.at_error, self.identity = 0, 0, None
        self.error: TsqlError | None = None
        self.messages: list[str] = []
        self.message_records: list[Message] = []
        self.unmapped_errors: list[TsqlError] = []     # failures the mapping did not cover, with the reserved number
        self.xact_abort = False
        self.statement_line: int | None = None         # the line of the statement running, when generated code says
        self._tries: list[int] = []                    # the call depth of each TRY block in progress
        self.values: dict[str, Any] = {}
        self.updated_columns: set[str] = set()
        self._scopes = [_Scope(procedure=None)]
        self._tx = TxManager(self)
        self._held: set[str] = set()
        self._lost: set[str] = set()
        self._trigger_stack: list[str] = []
        self._scratch_registered = False
        self._sequence = 0
        self._local_counters: dict[str, int] = {}
        self._scratch_targets: set[str] = set()
        self._heartbeat: _Heartbeat | None = None
        self._closed = False
        self.startup: recovery.Report = recovery.Report()      # what recovery on start did, and what it left for a person

    @classmethod
    def open(cls, spark: SparkSession, *, job: str, run: str, state_table: str, tables: Iterable[TableSpec] = (),
             settings=None, config: Config = Config(), errors=None) -> "SessionState":
        """Start a run: recover what earlier runs left unfinished before any other work, then start the heartbeat. `errors` is
        the source's half of the error mapping, the project's `error-mapping.json`."""
        ss = cls(spark, job=job, run=run, state_table=state_table, tables=tables, settings=settings, config=config,
                 errors=errors)
        mine = deltaops.marker(job, run)
        owner = spark.conf.get(deltaops.MARKER_CONF, None)
        if owner not in (None, "", mine):
            raise ValueError(f"This Spark session already writes commits as {owner!r}: the job run id marks every commit "
                             f"the session makes, so give each run its own session (spark.newSession()).")
        spark.conf.set(deltaops.MARKER_CONF, mine)
        ss.startup = recovery.start_up(spark, ss.state, config, job, run)
        if ss.startup.refused:
            warnings.warn("Recovery on start left unfinished transactions for manual reconciliation; writes to their tables "
                          "will fail until a person has reconciled them: " + " | ".join(ss.startup.refused), stacklevel=2)
        ss._heartbeat = _Heartbeat(ss)
        ss._heartbeat.start()
        return ss

    def __enter__(self) -> "SessionState":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ settings

    def setting(self, name: str) -> str:
        """The value of a named setting, such as a marked secret: `ss.setting("<identity>.secret_<n>")`."""
        return self.settings.secret(name)

    def runtime_identity(self, name: str) -> str:
        return self.settings.runtime_identity(name)

    @property
    def semantic(self) -> dict:
        return self.settings.semantic

    @property
    def default_collation(self) -> str:
        """The database's default collation: the settings file's (read from the source server) when it names the database,
        else the configured one."""
        entry = self.settings.semantic.get("databases", {}).get(self.config.database)
        return entry["default_collation"]["value"] if entry else self.config.default_collation

    # ------------------------------------------------------------------ small state

    @property
    def trancount(self) -> int:
        return self._tx.trancount

    def scope_identity(self) -> int | None:
        return self._scopes[-1].scope_identity

    def ident_current(self, table: str) -> int | None:
        spec = self._spec(table)
        ident = spec.identity_column
        if ident is None or ident.identity is None:
            return None
        if spec.target in self._scratch_targets:
            return self._local_counters.get(spec.target, ident.identity[0] - ident.identity[1])
        row = self.state.get("identity", spec.target)
        return int(row["value"]) if row else ident.identity[0]

    def dbts(self, database: str) -> int:
        """`@@DBTS`: the last row version handed out in a database, as a number."""
        row = self.state.get("rowversion", database)
        return int(row["value"]) if row else 0

    def print(self, text: str) -> None:
        self.messages.append(text)
        self.message_records.append(Message(0, 0, 1, text))

    def updated(self, column: str) -> bool:
        """`UPDATE(column)` inside a trigger."""
        return column.lower() in {c.lower() for c in self.updated_columns}

    @contextlib.contextmanager
    def handling(self, error: TsqlError) -> Iterator[None]:
        """Inside a CATCH block: `ERROR_NUMBER()` and the rest read the error being handled."""
        previous, self.error = self.error, error
        try:
            yield
        finally:
            self.error = previous

    # ------------------------------------------------------------------ errors (spec section 6)

    @property
    def errors_reserved_number(self) -> int:
        if self.errors is None:
            raise ErrorMappingNotLoaded("unmapped")
        return int(self.errors.reserved["number"])

    def fail(self, error_class: str, *, cause: BaseException | None = None, **details) -> TsqlError:
        """The error of an error class, as the source reports it. Sets the error number (`@@ERROR`); the caller raises it."""
        if self.errors is None:
            raise ErrorMappingNotLoaded(error_class)
        return self._error(self.errors.render(error_class, details), error_class, cause)

    def _error(self, rendered: Rendered, error_class: str | None, cause: BaseException | None) -> TsqlError:
        scope = self._scopes[-1]
        known = scope.procedure is not UNKNOWN
        error = TsqlError(rendered.number, rendered.severity, rendered.state, rendered.message,
                          error_class=None if rendered.unmapped else error_class, ends=rendered.ends,
                          catchable_at_same_level=rendered.catchable_at_same_level, exact_message=rendered.exact_message,
                          unmapped=rendered.unmapped, cause=cause, depth=len(self._scopes),
                          procedure=scope.procedure if known else None, procedure_known=known, line=self.statement_line)
        if rendered.unmapped:
            self.unmapped_errors.append(error)
        self.at_error = error.number
        return error

    def translate(self, exc: BaseException, *, operation: str | None = None,
                  details: "dict | Callable[[Classified], dict] | None" = None) -> TsqlError | None:
        """The error a failure stands for: a `TsqlError` as it is, the library's own `Failure` as its class, and a Spark or
        Delta failure through the target's mapping to a class and the source's mapping to its error, or to the reserved number
        when no rule covers it. None for anything else: a bug is not an error the program could have caught."""
        if isinstance(exc, TsqlError):
            if exc.depth is None:
                exc.depth = len(self._scopes)
            return exc
        if isinstance(exc, Failure):
            return self.fail(exc.error_class, **exc.details)
        if not isinstance(exc, Exception):
            return None
        classified = self.failures.classify(exc, operation)
        if classified is None:
            return None
        if self.errors is None:
            raise ErrorMappingNotLoaded(classified.error_class or "an unmapped failure") from exc
        if classified.error_class is None:
            return self._error(self.errors.unmapped(f"{classified.reason}.", classified.message), None, exc)
        extra = details(classified) if callable(details) else (details or {})
        return self._error(self.errors.render(classified.error_class, {**classified.details, **extra}),
                           classified.error_class, exc)

    def try_catch(self) -> "TryBlock":
        """`BEGIN TRY ... END TRY`. The body runs in a `with`; an error the CATCH would have caught is held, not raised, and the
        handler runs after the block (`if block.error: with ss.handling(block.error): ...`), where an error of its own is not
        caught by this TRY. An error T-SQL's CATCH would not have caught is raised again: a name that does not resolve at the
        same call depth as the TRY (finding 7.6), and anything that is not an error of the source at all."""
        return TryBlock(self)

    def guarded(self) -> "Guard":
        """One statement of a procedure that reads `@@ERROR` (ADR 0004). A failure records its number and the procedure goes on,
        or stops, as SQL Server does for that error: it goes on if the error ends only the statement, ends the procedure if
        it ends its scope, and ends everything if it ends the batch. Inside a TRY at the same call depth the error is the
        catch's, and with `XACT_ABORT` on every error ends the batch."""
        return Guard(self)

    def set_xact_abort(self, on: bool) -> None:
        """`SET XACT_ABORT`: with it on, an error ends the batch and a guarded statement lets it through."""
        self.xact_abort = bool(on)

    def raise_error(self, template: str, severity: int, state: int, *args, seterror: bool = False, log: bool = False) -> None:
        """`RAISERROR(template, severity, state, args)`. At severity 10 or below it is a message and returns; above that it
        raises error 50000 with the formatted text, ending only the statement."""
        if log:
            raise UnsupportedRaise("RAISERROR ... WITH LOG writes to the server's error log, which generated code cannot do; "
                                   "this is manual work.")
        severity = max(severity, 0)
        if severity > 18:
            raise self.fail("raise_severity_not_permitted")
        try:
            text = raiserror.format_text(template, args)
        except Failure as failure:
            raise self.fail(failure.error_class, **failure.details) from None
        state = raiserror.clamp_raise_state(state)
        if severity <= 10:
            self.messages.append(text)
            self.message_records.append(Message(50000, severity, state, text))
            self.at_error = 50000 if seterror else 0
            return
        raise self.fail("raised_error", message=text, severity=severity, state=state)

    def throw(self, number: int, message: str, state: int) -> None:
        """`THROW number, message, state`: always severity 16, and it ends the batch."""
        if number < 50000:
            raise self.fail("throw_number_out_of_range", number=number)
        raise self.fail("thrown_error", number=number, message=raiserror.throw_text(message), state=state % 256)

    def rethrow(self) -> None:
        """`THROW;` inside a CATCH block: the error being handled, unchanged, ending the batch."""
        handled = self.error
        if handled is None:
            raise RethrowOutsideHandler("THROW without arguments outside a CATCH block: SQL Server refuses it when it compiles "
                                        "the batch, so generated code never has one.")
        again = TsqlError(handled.number, handled.severity, handled.state, handled.message, error_class=handled.error_class,
                          ends="batch", exact_message=handled.exact_message, unmapped=handled.unmapped, cause=handled,
                          depth=len(self._scopes), procedure=handled.procedure, procedure_known=handled.procedure_known,
                          line=handled.line)
        self.at_error = handled.number
        raise again

    def error_number(self) -> int | None:
        return None if self.error is None else self.error.number

    def error_severity(self) -> int | None:
        return None if self.error is None else self.error.severity

    def error_state(self) -> int | None:
        return None if self.error is None else self.error.state

    def error_message(self) -> str | None:
        return None if self.error is None else self.error.message

    def error_line(self) -> int | None:
        """`ERROR_LINE()`: the line generated code said it was running when the error was raised."""
        if self.error is None:
            return None
        if self.error.line is None:
            raise ErrorFieldUnknown("ERROR_LINE() was read for an error raised where generated code never said which line it "
                                    "ran (set ss.statement_line before the statement).")
        return self.error.line

    def error_procedure(self) -> str | None:
        """`ERROR_PROCEDURE()`: the procedure the error was raised in, None for a batch."""
        if self.error is None:
            return None
        if not self.error.procedure_known:
            raise ErrorFieldUnknown("ERROR_PROCEDURE() was read for an error raised in a call depth that generated code never "
                                    "named (ss.scope(<name>)).")
        return self.error.procedure

    def error_field(self, name: str):
        """One field of the error being handled: number, message, severity, state, line or procedure."""
        return getattr(self, f"error_{name.lower()}")()

    # ------------------------------------------------------------------ call depth, temp tables and scratch tables

    @contextlib.contextmanager
    def scope(self, procedure: "str | None | Any" = UNKNOWN) -> Iterator[None]:
        """A call depth: a procedure, function or trigger, named for ERROR_PROCEDURE(). Temp tables created inside are dropped
        when it ends. An error that leaves it is an error of this call depth: a TRY beside the statement that failed does not
        see a name that did not resolve, and the TRY of the caller does (finding 7.6). A guarded statement that ended the
        procedure ends it here, and the caller goes on."""
        scope = _Scope(procedure=procedure)
        self._scopes.append(scope)
        try:
            yield
        except ScopeAborted as stopped:
            self.at_error = stopped.error.number
        except Exception as exc:
            error = self.translate(exc)
            if error is None or error is exc:
                raise
            raise error from exc
        finally:
            self._scopes.pop()
            for spec in scope.temps.values():
                self._drop_physical(spec.target)
            for name in scope.owned:
                self._drop_physical(name)

    def tables_in_scope(self) -> list[str]:
        return sorted(self._scopes[-1].names.values())

    def _register_scratch(self) -> None:
        if self._scratch_registered:
            return
        self.spark.sql(f"CREATE SCHEMA IF NOT EXISTS {self.scratch_schema}")
        self.state.put("scratch", f"{self.scratch_schema}|{self.prefix}", job=self.job, run=self.run,
                       heartbeat=self.clock())
        self._scratch_registered = True

    def _scratch_name(self, hint: str) -> str:
        self._register_scratch()
        self._sequence += 1
        return f"{self.scratch_schema}.{self.prefix}{re.sub(r'[^0-9A-Za-z]', '', hint)}_{self._sequence}"

    def _drop_physical(self, name: str) -> None:
        self._tx.forget(name)
        self._scratch_targets.discard(name)
        self.spark.sql(f"DROP TABLE IF EXISTS {name}")

    def snapshot(self, df: DataFrame) -> DataFrame:
        """Fix a DataFrame's rows in a scratch Delta table, for a value read after a write to one of its sources. Spark's
        cache() is never used for this: a cached DataFrame can be evicted and recomputed."""
        name = self._scratch_name("snap")
        df.write.format("delta").saveAsTable(name)
        self._scopes[-1].owned.append(name)
        return self.spark.table(name)

    def _snapshot_for_statement(self, df: DataFrame, owned: list[str]) -> DataFrame:
        name = self._scratch_name("stmt")
        df.write.format("delta").saveAsTable(name)
        owned.append(name)
        return self.spark.table(name)

    def create_temp_table(self, spec: TableSpec) -> None:
        self._declare(spec, transactional=True)

    def create_table_variable(self, spec: TableSpec) -> None:
        self._declare(spec, transactional=False)

    def _declare(self, spec: TableSpec, transactional: bool) -> None:
        scope = self._scopes[-1]
        key = spec.target.lower()
        if key in scope.temps:
            raise self.fail("object_already_exists", name=spec.target)
        physical = self._scratch_name(re.sub(r"[^0-9A-Za-z]", "", spec.target) or "t")
        columns = ", ".join(f"`{c.name}` {c.type}" for c in spec.columns)
        self.spark.sql(f"CREATE TABLE {physical} ({columns}) USING DELTA")
        scope.temps[key] = dataclasses.replace(spec, target=physical, transactional=transactional)
        scope.names[key] = spec.target
        self._scratch_targets.add(physical)
        if transactional:
            self._tx.created(physical)

    def drop_temp_table(self, name: str) -> None:
        for scope in reversed(self._scopes):
            if name.lower() in scope.temps:
                if self._tx.real is not None:
                    raise ManualWrite(f"DROP TABLE {name} inside a transaction: a rollback would have to bring the table back; "
                                      f"this is manual work.")
                self._drop_physical(scope.temps.pop(name.lower()).target)
                del scope.names[name.lower()]
                return
        raise self.fail("drop_target_not_found", name=name)

    # ------------------------------------------------------------------ resolving names

    def _spec(self, name: str) -> TableSpec:
        for scope in reversed(self._scopes):
            if name.lower() in scope.temps:
                return scope.temps[name.lower()]
        found = self.catalog.get(name.lower())
        if found is not None:
            return found
        if name.startswith(("#", "@")):
            raise self.fail("object_not_found", name=name)
        raise UnknownTable(f"{name} is not described in the catalog the session was given: the wrappers write only to "
                           f"tables whose constraints they know.")

    def table(self, name: str) -> DataFrame:
        """A table, a temp table or a table variable, by its name."""
        if name.startswith(("#", "@")) or name.lower() in self.catalog:
            return self._read(self._spec(name))
        return self.spark.table(name)

    def _read(self, spec: TableSpec) -> DataFrame:
        return self.spark.table(spec.target)

    # ------------------------------------------------------------------ leases

    def _lease(self, spec: TableSpec) -> None:
        """Take the write lease on a table before the first write to it."""
        if spec.target in self._scratch_targets:
            return
        if self._lost or (self._heartbeat and self._heartbeat.error):
            why = (f"its heartbeat failed ({self._heartbeat.error})" if self._heartbeat and self._heartbeat.error
                   else "it was taken away by a forced recovery or an expiry")
            raise LeaseLost(f"This run's write lease on {sorted(self._lost) or [spec.target]} was lost: {why}. Nothing more "
                            f"may be written, because another writer may hold the table.")
        if spec.target in self._held:
            return
        started = self.clock()
        while True:
            row = self.state.get("lease", spec.target)
            if row is not None and row["run"] != self.run:
                now = self.clock()
                if now - row["hb"] < self.config.lease_expiry_seconds:                 # live
                    error = LeaseHeld(spec.target, row["job"], row["run"], row["hb"] + self.config.lease_expiry_seconds, now)
                    if row["job"] != self.job or self.config.restart_wait <= 0:
                        raise error
                    if now - started >= self.config.restart_wait:
                        raise LeaseWaitTimeout(spec.target, row["job"], row["run"], error.expires_at, now, now - started)
                    time.sleep(self.config.poll_seconds)
                    continue
                theirs = recovery.entries_by_run(self.state).get(row["run"], [])        # expired: first undo what it left
                recovery.recover_run(self.spark, self.state, row["run"], theirs, recovery.Report())
                continue
            if row is None:
                self.state.insert_if_absent("lease", spec.target, job=self.job, run=self.run, heartbeat=self.clock())
                # Two writers can both insert: Delta lets concurrent inserts through. The one with the smallest run id
                # keeps the lease, the other withdraws its row and finds it held.
                rows = sorted(self.state.rows(f"kind = 'lease' AND key = {lit(spec.target)}"), key=lambda r: r["run"])
                if len(rows) > 1 and rows[0]["run"] != self.run:
                    self.state.delete(f"kind = 'lease' AND key = {lit(spec.target)} AND run = {lit(self.run)}")
                    continue
                if len(rows) > 1:
                    self.state.delete(f"kind = 'lease' AND key = {lit(spec.target)} AND run <> {lit(self.run)}")
                row = self.state.get("lease", spec.target)
            if row is not None and row["run"] == self.run:
                self._held.add(spec.target)
                return

    # ------------------------------------------------------------------ counters

    def _counter_missing_but_rows(self, spec: TableSpec, kind: str, key: str) -> bool:
        return self.state.get(kind, key) is None and self._read(spec).limit(1).count() > 0

    def _next_identity(self, spec: TableSpec, n: int) -> int:
        """Reserve n identity values, whatever happens to the statement after: a rollback never resets the counter."""
        ident = spec.identity_column
        assert ident is not None and ident.identity is not None
        seed, step = ident.identity
        if spec.target in self._scratch_targets:
            old = self._local_counters.get(spec.target, seed - step)
            self._local_counters[spec.target] = old + n * step
            return old
        if self._counter_missing_but_rows(spec, "identity", spec.target):
            raise CounterNotInitialised(spec.target)
        return self.state.advance("identity", spec.target, n, step, seed - step, self.job, self.run)

    def _next_rowversions(self, spec: TableSpec, n: int) -> int:
        if self._counter_missing_but_rows(spec, "rowversion", spec.database):
            raise CounterNotInitialised(spec.target)
        return self.state.advance("rowversion", spec.database, n, 1, 0, self.job, self.run)

    def transaction(self) -> Transaction:
        """BEGIN TRANSACTION, as a block: `with ss.transaction() as tx:`. Leaving it commits, an exception rolls back."""
        return Transaction(self)

    # ------------------------------------------------------------------ writes: the public statements

    def insert(self, table: str, df: DataFrame, *, identity_insert: bool = False) -> None:
        """INSERT ... SELECT."""
        spec = self._spec(table)
        self._statement(spec, "insert", lambda: self._run_insert(spec, df, identity_insert))

    def insert_values(self, table: str, rows: list[dict], *, identity_insert: bool = False) -> None:
        """INSERT ... VALUES: one dictionary per row, by column name; a column left out takes its default."""
        spec = self._spec(table)
        names: list[str] = []
        for r in rows:
            for k in r:
                if k not in names:
                    names.append(k)
        if not names:
            raise ValueError("insert_values needs at least one column")
        for k in names:
            if not spec.has_column(k):
                raise self.fail("column_not_found", name=k)
        schema = ", ".join(f"`{spec.column(k).name}` {spec.column(k).type}" for k in names)
        df = self.spark.createDataFrame([tuple(r.get(k) for k in names) for r in rows], schema)
        self._statement(spec, "insert", lambda: self._run_insert(spec, df, identity_insert))

    def update(self, table: str, *, set: dict[str, Any], where: "str | SparkColumn | None" = None) -> None:
        """UPDATE ... SET ... WHERE."""
        spec = self._spec(table)
        touched = {spec.column(c).name for c in set} if all(spec.has_column(c) for c in set) else None
        if touched is None:
            bad = next(c for c in set if not spec.has_column(c))
            self.rowcount = 0
            raise self.fail("column_not_found", name=bad)
        self.updated_columns = {*touched}
        cond = F.expr(where) if isinstance(where, str) else where
        assignments = {spec.column(c).name: (v if isinstance(v, SparkColumn) else F.lit(v)) for c, v in set.items()}

        def body() -> int:
            base = self._read(spec)
            matched = base.where(F.coalesce(cond, F.lit(False))) if cond is not None else base
            pairs = matched.select(*[F.col(f"`{c.name}`").alias(f"o_{c.name}") for c in spec.columns],
                                   *[(assignments[c.name] if c.name in assignments else F.col(f"`{c.name}`")
                                      ).cast(c.type).alias(f"n_{c.name}") for c in spec.columns])
            return self._run_update(spec, pairs, touched, fast=(assignments, cond))

        self._statement(spec, "update", body, touched=touched)

    def delete(self, table: str, *, where: "str | SparkColumn | None" = None) -> None:
        """DELETE ... WHERE."""
        spec = self._spec(table)
        cond = F.expr(where) if isinstance(where, str) else where

        def body() -> int:
            base = self._read(spec)
            old = base.where(F.coalesce(cond, F.lit(False))) if cond is not None else base
            return self._run_delete(spec, old, fast=(cond,))

        self._statement(spec, "delete", body)

    def _statement(self, spec: TableSpec, event: str, body: Callable[[], int], touched=None) -> None:
        multi = self._writes_several_tables(spec, event, touched)
        try:
            with (self._tx.begin_statement() if multi else contextlib.nullcontext()):
                n = body()
                self.rowcount, self.at_error = n, 0
        except TsqlError as err:
            self.at_error, self.rowcount = err.number, 0
            raise
        except Exception as exc:                          # a Failure of the library's, or a failure of Spark or Delta
            error = self.translate(exc, operation=event, details=lambda c: self._write_details(spec, event, c))
            if error is None:
                raise
            self.rowcount = 0
            raise error from exc

    def _write_details(self, spec: TableSpec, event: str, classified: Classified) -> dict:
        """What a failure of Delta cannot say about the write it failed: the table, the statement and the catalog's spelling of
        a constraint's name (Delta folds it to lower case)."""
        verb = event.upper()
        if classified.error_class == "not_null_violation":
            return {"table": f"{spec.database}.{spec.schema}.{spec.name}", "verb": verb}
        if classified.error_class == "check_violation":
            named = classified.details.get("constraint", "").lower()
            check = next((c for c in spec.checks if c.name.lower() == named), None)
            columns = check.columns if check else ()
            return {"verb": verb, "database": spec.database, "table": spec.qualified,
                    **({"constraint": check.name} if check else {}), **({"column": columns[0]} if len(columns) == 1 else {})}
        return {}

    def _writes_several_tables(self, spec: TableSpec, event: str, touched) -> bool:
        if spec.triggers_for(event, "after") or spec.triggers_for(event, "instead_of"):
            return True
        action = "on_delete" if event == "delete" else "on_update"
        return event != "insert" and any(getattr(fk, action) != "NO ACTION" for _, fk in self.incoming.get(spec.target.lower(), []))

    # ------------------------------------------------------------------ INSERT

    def _run_insert(self, spec: TableSpec, df: DataFrame, identity_insert: bool) -> int:
        given: dict[str, str] = {}
        for c in df.columns:
            if not spec.has_column(c):
                raise self.fail("column_not_found", name=c)
            given[spec.column(c).name] = c
        ident, rowversion = spec.identity_column, spec.rowversion_column
        if ident and ident.name in given and not identity_insert:
            raise self.fail("identity_insert_not_allowed", table=spec.name)
        owned: list[str] = []
        try:
            filled = df.select(
                *[(F.col(f"`{given[c.name]}`").cast(c.type) if c.name in given else
                   (F.expr(c.default).cast(c.type) if c.default else F.lit(None).cast(c.type))).alias(c.name)
                  for c in spec.columns], F.monotonically_increasing_id().alias(ORD))
            if spec.triggers_for("insert", "instead_of"):
                snap = self._snapshot_for_statement(filled, owned)
                new = snap.drop(ORD)
                n = new.count()
                self._fire(spec, "insert", "instead_of", new, new.limit(0), n)
                return n
            self._lease(spec)
            self._tx.register(spec)
            snap = self._snapshot_for_statement(filled, owned)
            n = snap.count()
            numbered = snap.withColumn("__n", F.row_number().over(Window.orderBy(ORD)))
            if ident and ident.name not in given and n:
                assert ident.identity is not None
                seed, step = ident.identity
                base = self._next_identity(spec, n)
                numbered = numbered.withColumn(ident.name, (F.lit(base) + F.col("__n") * F.lit(step)).cast(ident.type))
            if rowversion and n:
                first = self._next_rowversions(spec, n)
                numbered = numbered.withColumn(rowversion.name, F.unhex(F.lpad(F.hex(
                    (F.lit(first) + F.col("__n")).cast("bigint")), 16, "0")))
            new = numbered.select(*spec.names)
            if n:
                default = self.default_collation
                constraints.check_not_null(spec, new, "INSERT")
                constraints.check_checks(spec, new, "INSERT")
                existing = self._read(spec)
                constraints.check_unique(spec, new, existing, default)
                constraints.check_foreign_keys(spec, new, existing.unionByName(new), "INSERT", self._spec, self._read, default)
                new.write.format("delta").mode("append").saveAsTable(spec.target)
                if ident and ident.identity:
                    seed, step = ident.identity
                    pick = F.max if step > 0 else F.min
                    last = new.agg(pick(F.col(f"`{ident.name}`"))).collect()[0][0]
                    if ident.name in given and spec.target not in self._scratch_targets:
                        self.state.raise_to("identity", spec.target, int(last), step, seed, self.job, self.run)
                    elif ident.name in given:
                        self._local_counters[spec.target] = max(self._local_counters.get(spec.target, int(last)), int(last))
                    self._scopes[-1].scope_identity, self.identity = int(last), int(last)
            self.updated_columns = {*spec.names}
            self._fire(spec, "insert", "after", new, new.limit(0), n)
            return n
        finally:
            for name in owned:
                self._drop_physical(name)

    # ------------------------------------------------------------------ UPDATE

    def _identity_condition(self, spec: TableSpec, left: str, right: str, right_prefix: str = "") -> SparkColumn:
        columns = list(spec.row_identity or spec.names)
        return constraints.all_of([F.col(f"{left}.`{c}`").eqNullSafe(F.col(f"{right}.`{right_prefix}{c}`"))
                                                     for c in columns])

    def _rest(self, spec: TableSpec, pairs: DataFrame) -> DataFrame:
        """The table's rows an update or delete leaves alone."""
        old = pairs.select(*[F.col(f"o_{c}").alias(c) for c in spec.names])
        return self._read(spec).alias("b").join(old.alias("o"), self._identity_condition(spec, "b", "o"), "left_anti")

    def _run_update(self, spec: TableSpec, pairs: DataFrame, touched: set[str], fast=None) -> int:
        """Update the rows `pairs` describes, each with its old (`o_`) and new (`n_`) values: check, write, cascade, fire."""
        ident, rowversion = spec.identity_column, spec.rowversion_column
        if ident and ident.name in touched:
            raise self.fail("identity_update_not_allowed", column=ident.name)
        self._lease(spec)
        incoming = self.incoming.get(spec.target.lower(), [])
        low = {t.lower() for t in touched}
        cascades = [(c, fk) for c, fk in incoming if fk.on_update != "NO ACTION" and low & {r.lower() for r in fk.ref_columns}]
        instead = spec.triggers_for("update", "instead_of")
        after = spec.triggers_for("update", "after")
        slow = bool(instead or after or cascades or rowversion or fast is None)
        owned: list[str] = []
        try:
            if slow:
                if rowversion:
                    pairs = pairs.withColumn(ORD, F.monotonically_increasing_id())
                pairs = self._snapshot_for_statement(pairs, owned)
            n = pairs.count()
            if instead:
                self._fire(spec, "update", "instead_of", self._image(spec, pairs, "n_"), self._image(spec, pairs, "o_"), n)
                return n
            if n == 0 and not after:
                return 0
            if n:
                self._tx.register(spec)
                if rowversion:
                    first = self._next_rowversions(spec, n)
                    pairs = pairs.withColumn(f"n_{rowversion.name}", F.unhex(F.lpad(F.hex(
                        (F.lit(first) + F.row_number().over(Window.orderBy(ORD))).cast("bigint")), 16, "0")))
                new, old = self._image(spec, pairs, "n_"), self._image(spec, pairs, "o_")
                self._check_update(spec, pairs, new, touched)
                if slow:
                    self._write_update_merge(spec, pairs, touched | ({rowversion.name} if rowversion else set()))
                else:
                    assignments, cond = fast  # type: ignore[misc]
                    target = DeltaTable.forName(self.spark, spec.target)
                    if cond is None:
                        deltaops.retrying(lambda: target.update(set=assignments))
                    else:
                        deltaops.retrying(lambda: target.update(F.coalesce(cond, F.lit(False)), assignments))
                for child, fk in cascades:
                    self._cascade_update(spec, child, fk, pairs)
            else:
                new, old = self._image(spec, pairs, "n_"), self._image(spec, pairs, "o_")
            self._fire(spec, "update", "after", new, old, n)
            return n
        finally:
            for name in owned:
                self._drop_physical(name)

    def _image(self, spec: TableSpec, pairs: DataFrame, prefix: str) -> DataFrame:
        return pairs.select(*[F.col(f"`{prefix}{c}`").alias(c) for c in spec.names])

    def _check_update(self, spec: TableSpec, pairs: DataFrame, new: DataFrame, touched: set[str]) -> None:
        low = {t.lower() for t in touched}
        default = self.default_collation
        relevant = dataclasses.replace(
            spec,
            columns=tuple(c if c.name.lower() in low else dataclasses.replace(c, nullable=True) for c in spec.columns),
            checks=tuple(ck for ck in spec.checks if not ck.columns or low & {c.lower() for c in ck.columns}),
            uniques=tuple(u for u in spec.uniques if low & {c.lower() for c in u.columns}),
            foreign_keys=tuple(fk for fk in spec.foreign_keys if low & {c.lower() for c in fk.columns}))
        constraints.check_not_null(relevant, new, "UPDATE")
        constraints.check_checks(relevant, new, "UPDATE")
        rest = self._rest(spec, pairs)
        if relevant.uniques:
            constraints.check_unique(relevant, new, rest, default)
        if relevant.foreign_keys:
            constraints.check_foreign_keys(relevant, new, rest.unionByName(new), "UPDATE", self._spec, self._read, default)
        for child, fk in self.incoming.get(spec.target.lower(), []):
            if not low & {r.lower() for r in fk.ref_columns}:
                continue
            lost = self._lost_keys(spec, fk, pairs, rest.unionByName(new))
            kids = self._children(spec, child, fk, lost, rest)
            if fk.on_update == "NO ACTION" and kids.limit(1).count():
                raise constraints.reference_error(spec, child, fk, "UPDATE")

    def _lost_keys(self, spec: TableSpec, fk: ForeignKey, pairs: DataFrame, final: DataFrame) -> DataFrame:
        """The old key values of the parent rows that no longer exist after the statement."""
        default = self.default_collation
        parent_columns = [spec.column(c) for c in fk.ref_columns]
        old = pairs.select(*[constraints.normalised(spec, c, F.col(f"`o_{c.name}`"), default).alias(f"k{i}")
                             for i, c in enumerate(parent_columns)],
                           *[F.col(f"`o_{c.name}`").alias(f"v{i}") for i, c in enumerate(parent_columns)],
                           *[F.col(f"`n_{c.name}`").alias(f"w{i}") for i, c in enumerate(parent_columns)])
        keys = final.select(*[constraints.normalised(spec, c, F.col(f"`{c.name}`"), default).alias(f"k{i}")
                              for i, c in enumerate(parent_columns)])
        cond = constraints.all_of([F.col(f"a.k{i}").eqNullSafe(F.col(f"f.k{i}")) for i in range(len(parent_columns))])
        return old.alias("a").join(keys.alias("f"), cond, "left_anti")

    def _children(self, spec: TableSpec, child: TableSpec, fk: ForeignKey, lost: DataFrame, rest: DataFrame) -> DataFrame:
        """The child rows that reference a lost key."""
        default = self.default_collation
        rows = rest if child.target == spec.target else self._read(child)
        cond = constraints.all_of([
            constraints.normalised(spec, spec.column(rc), F.col(f"c.`{c}`"), default) == F.col(f"l.k{i}")
            for i, (c, rc) in enumerate(zip(fk.columns, fk.ref_columns))])
        return rows.alias("c").join(lost.alias("l"), cond, "inner")

    def _write_update_merge(self, spec: TableSpec, pairs: DataFrame, touched: set[str]) -> None:
        if not spec.row_identity and (spec.triggers_for("update", "after") or spec.triggers_for("update", "instead_of")
                                      or spec.rowversion_column):
            raise ManualWrite(f"UPDATE on {spec.target}: the table has no primary key, so the rows a trigger or a row version "
                              f"needs cannot be told apart. This write is manual work.")
        source = pairs.select(*[F.col(f"`o_{c}`") for c in (spec.row_identity or spec.names)],
                              *[F.col(f"`n_{c.name}`") for c in spec.columns if c.name in touched]).distinct()
        cond = constraints.all_of([F.col(f"t.`{c}`").eqNullSafe(F.col(f"s.`o_{c}`"))
                                                     for c in (spec.row_identity or spec.names)])
        deltaops.retrying(lambda: DeltaTable.forName(self.spark, spec.target).alias("t").merge(source.alias("s"), cond)
                          .whenMatchedUpdate(set={c.name: F.col(f"s.`n_{c.name}`") for c in spec.columns if c.name in touched})
                          .execute())

    def _cascade_update(self, spec: TableSpec, child: TableSpec, fk: ForeignKey, pairs: DataFrame) -> None:
        default = self.default_collation
        changed_cond = functools.reduce(lambda a, b: a | b, [~F.col(f"`o_{c}`").eqNullSafe(F.col(f"`n_{c}`")) for c in fk.ref_columns])
        changed = pairs.where(changed_cond)
        cond = constraints.all_of([
            constraints.normalised(spec, spec.column(rc), F.col(f"c.`{c}`"), default)
            == constraints.normalised(spec, spec.column(rc), F.col(f"p.`o_{rc}`"), default)
            for c, rc in zip(fk.columns, fk.ref_columns)])
        kids = self._read(child).alias("c").join(changed.alias("p"), cond, "inner")
        replacement: dict[str, SparkColumn] = {}
        for c, rc in zip(fk.columns, fk.ref_columns):
            canonical = child.column(c)
            if fk.on_update == "CASCADE":
                replacement[canonical.name] = F.col(f"p.`n_{rc}`")
            elif fk.on_update == "SET NULL":
                replacement[canonical.name] = F.lit(None)
            else:
                replacement[canonical.name] = F.expr(canonical.default) if canonical.default else F.lit(None)
        child_pairs = kids.select(*[F.col(f"c.`{c.name}`").alias(f"o_{c.name}") for c in child.columns],
                                  *[(replacement[c.name] if c.name in replacement else F.col(f"c.`{c.name}`")).cast(c.type)
                                    .alias(f"n_{c.name}") for c in child.columns])
        saved = self.rowcount
        self._run_update(child, child_pairs, set(replacement))
        self.rowcount = saved

    # ------------------------------------------------------------------ DELETE

    def _run_delete(self, spec: TableSpec, old: DataFrame, fast=None) -> int:
        self._lease(spec)
        incoming = self.incoming.get(spec.target.lower(), [])
        instead = spec.triggers_for("delete", "instead_of")
        after = spec.triggers_for("delete", "after")
        slow = bool(instead or after or incoming or fast is None)
        owned: list[str] = []
        try:
            if slow:
                old = self._snapshot_for_statement(old, owned)
            n = old.count()
            empty = old.limit(0)
            if instead:
                self._fire(spec, "delete", "instead_of", empty, old, n)
                return n
            if n == 0 and not after:
                return 0
            if n:
                self._tx.register(spec)
                pairs = old.select(*[F.col(f"`{c}`").alias(f"o_{c}") for c in spec.names],
                                   *[F.col(f"`{c}`").alias(f"n_{c}") for c in spec.names])
                rest = self._rest(spec, pairs)
                actions = []
                for child, fk in incoming:
                    lost = self._lost_keys(spec, fk, pairs, rest)
                    kids = self._children(spec, child, fk, lost, rest)
                    if fk.on_delete == "NO ACTION":
                        if kids.limit(1).count():
                            raise constraints.reference_error(spec, child, fk, "DELETE")
                    else:
                        actions.append((child, fk, kids))
                if slow:
                    self._write_delete_merge(spec, old)
                else:
                    assert fast is not None
                    (cond,) = fast
                    target = DeltaTable.forName(self.spark, spec.target)
                    if cond is None:
                        deltaops.retrying(lambda: target.delete())
                    else:
                        deltaops.retrying(lambda: target.delete(F.coalesce(cond, F.lit(False))))
                for child, fk, kids in actions:
                    self._cascade_delete(child, fk, kids)
            self._fire(spec, "delete", "after", empty, old, n)
            return n
        finally:
            for name in owned:
                self._drop_physical(name)

    def _write_delete_merge(self, spec: TableSpec, old: DataFrame) -> None:
        columns = list(spec.row_identity or spec.names)
        source = old.select(*[F.col(f"`{c}`").alias(f"o_{c}") for c in columns]).distinct()
        cond = constraints.all_of([F.col(f"t.`{c}`").eqNullSafe(F.col(f"s.`o_{c}`")) for c in columns])
        deltaops.retrying(lambda: DeltaTable.forName(self.spark, spec.target).alias("t").merge(source.alias("s"), cond)
                          .whenMatchedDelete().execute())

    def _cascade_delete(self, child: TableSpec, fk: ForeignKey, kids: DataFrame) -> None:
        rows = kids.select(*[F.col(f"c.`{c.name}`").alias(c.name) for c in child.columns])
        saved = self.rowcount
        if fk.on_delete == "CASCADE":
            self._run_delete(child, rows)
        else:
            replacement = {}
            for c in fk.columns:
                default = child.column(c).default
                replacement[child.column(c).name] = F.expr(default) if default and fk.on_delete == "SET DEFAULT" else F.lit(None)
            pairs = rows.select(*[F.col(f"`{c.name}`").alias(f"o_{c.name}") for c in child.columns],
                                *[(replacement[c.name] if c.name in replacement else F.col(f"`{c.name}`")).cast(c.type)
                                  .alias(f"n_{c.name}") for c in child.columns])
            self._run_update(child, pairs, set(replacement))
        self.rowcount = saved

    # ------------------------------------------------------------------ triggers

    def _fire(self, spec: TableSpec, event: str, timing: str, inserted: DataFrame, deleted: DataFrame, n: int) -> None:
        for trigger in spec.triggers_for(event, timing):
            if not self.config.nested_triggers and self._trigger_stack:
                return
            if self._trigger_stack and self._trigger_stack[-1] == spec.target and not self.config.recursive_triggers:
                return                                                 # direct recursion is off unless the database says so
            if len(self._trigger_stack) >= TRIGGER_NESTING_LIMIT:
                raise self.fail("nesting_level_exceeded")
            updated = self.updated_columns
            self._trigger_stack.append(spec.target)
            try:
                with self.scope():
                    self.rowcount = n
                    self.updated_columns = updated
                    trigger.function(self, inserted, deleted)
            except BaseException:
                self._tx.doom()
                raise
            finally:
                self._trigger_stack.pop()
                self.updated_columns = updated
            self.rowcount = n

    # ------------------------------------------------------------------ ending

    def abandon(self) -> None:
        """Stop the heartbeat and nothing else: what a crash looks like. For the library's own crash tests."""
        if self._heartbeat:
            self._heartbeat.stop()
        self._closed = True

    def close(self) -> None:
        """End the run: roll back an open transaction, drop the scratch tables and release the leases."""
        if self._closed:
            return
        if self._heartbeat:
            self._heartbeat.stop()
        try:
            if self._tx.real is not None:
                self._tx.rollback()
        finally:
            for scope in self._scopes:
                for spec in list(scope.temps.values()):
                    self._drop_physical(spec.target)
            if self._scratch_registered:
                for t in self.spark.catalog.listTables(self.scratch_schema):
                    if t.name.startswith(self.prefix):
                        self.spark.sql(f"DROP TABLE IF EXISTS {self.scratch_schema}.{t.name}")
            self.state.delete(f"run = {lit(self.run)} AND kind IN ('lease', 'tx', 'scratch')")
            self._closed = True
        if self._heartbeat and self._heartbeat.error:                      # a run that never wrote again must hear of it too
            raise LeaseLost(f"This run's heartbeat failed ({self._heartbeat.error}), so its leases may have expired while it ran.")


class TryBlock:
    """`BEGIN TRY`: see `SessionState.try_catch`. `error` is the error the CATCH block handles, None if the body went through."""

    def __init__(self, ss: SessionState):
        self.ss, self.error = ss, None
        self._depth = len(ss._scopes)

    def __enter__(self) -> "TryBlock":
        self.ss._tries.append(self._depth)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.ss._tries.pop()
        if exc is None or not isinstance(exc, Exception):
            return False
        error = self.ss.translate(exc)
        if error is None:
            return False                                           # not an error of the source: a bug, never caught
        if not error.catchable_at_same_level and error.depth == self._depth:
            if error is exc:
                return False
            raise error from exc                                   # a CATCH beside the failing statement does not see it
        self.error = error
        self.ss.at_error = error.number
        return True


class Guard:
    """One guarded statement: see `SessionState.guarded`."""

    def __init__(self, ss: SessionState):
        self.ss = ss

    def __enter__(self) -> "Guard":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        ss = self.ss
        if exc is None:
            ss.at_error = 0
            return False
        if not isinstance(exc, Exception) or isinstance(exc, ScopeAborted):
            return False
        error = ss.translate(exc)
        if error is None:
            return False
        ss.at_error = error.number
        inside_try = bool(ss._tries) and ss._tries[-1] == len(ss._scopes)
        if inside_try or ss.xact_abort or error.ends == "batch" or (error.ends == "scope" and len(ss._scopes) == 1):
            if error is exc:
                return False
            raise error from exc
        if error.ends == "scope":
            raise ScopeAborted(error) from exc
        return True                                                # the statement ended and the procedure goes on
