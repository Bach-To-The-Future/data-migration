"""The errors of the support library.

`TsqlError` is an error SQL Server would raise, with its number, severity, state and text. The rest are the library's own
named errors: each says what went wrong and what to do, and none is ever swallowed (CODING_STANDARDS.md: nothing
degrades silently)."""
from __future__ import annotations

import datetime


class TsqlError(Exception):
    """An error as the source reports it: its number, severity, state and text, and what the mapping knows of it.

    `error_class` is the core's neutral class it came from (None when the code raised it with a number of its own);
    `ends` says how far it reaches (statement, scope or batch); `catchable_at_same_level` is False for an error a CATCH
    beside the failing statement does not see; `exact_message` is False when the failure did not carry what the source's
    message needs, so the text is a stand-in; `unmapped` is True when the source has no entry for the failure and the
    error carries its reserved number. `procedure` and `line` are where it was raised, when generated code said so;
    `procedure_known` is False when the call depth it was raised in was never named, so ERROR_PROCEDURE() cannot say;
    `depth` is the call depth it was raised at."""

    def __init__(self, number: int, severity: int, state: int, message: str, *, error_class: str | None = None,
                 ends: str = "statement", catchable_at_same_level: bool = True, exact_message: bool = True,
                 unmapped: bool = False, cause: BaseException | None = None, depth: int | None = None,
                 procedure: str | None = None, procedure_known: bool = True, line: int | None = None):
        super().__init__(message)
        self.number, self.severity, self.state, self.message = number, severity, state, message
        self.error_class, self.ends, self.catchable_at_same_level = error_class, ends, catchable_at_same_level
        self.exact_message, self.unmapped, self.cause = exact_message, unmapped, cause
        self.depth, self.procedure, self.procedure_known, self.line = depth, procedure, procedure_known, line


class SupportLibraryError(Exception):
    """The base of the library's own named errors."""


class Failure(SupportLibraryError):
    """A failure the library found in a write, named by its neutral error class and its details, not yet turned into the
    source's error. Session state turns it into a `TsqlError` before it reaches generated code."""

    def __init__(self, error_class: str, **details):
        super().__init__(f"{error_class}: {details}")
        self.error_class, self.details = error_class, details


class ErrorMappingNotLoaded(SupportLibraryError):
    """An error needed the source's half of the error mapping and session state was given none."""

    def __init__(self, error_class: str):
        super().__init__(f"The error class {error_class!r} needs the source's error mapping to become an error, and this "
                         f"session was opened without one. Open it with errors=<the project's error-mapping.json>.")


class MissingDetail(SupportLibraryError):
    """An error class was raised without a detail the source's message needs, and the mapping has no stand-in text."""

    def __init__(self, error_class: str, missing: list[str]):
        super().__init__(f"The error class {error_class!r} was raised without {missing}, which its message needs.")
        self.error_class, self.missing = error_class, missing


class ErrorFieldUnknown(SupportLibraryError):
    """Generated code read ERROR_LINE() or ERROR_PROCEDURE() of an error whose line or procedure it never said."""


class RethrowOutsideHandler(SupportLibraryError):
    """A rethrow (`THROW;`) outside a CATCH block, which SQL Server refuses when it compiles the batch: generated code never
    emits one, so this is a defect in generation."""


class ScopeAborted(SupportLibraryError):
    """A guarded statement failed with an error that ends its procedure: the rest of the procedure does not run and the
    caller goes on. The scope that the procedure runs in ends it; nothing else catches this."""

    def __init__(self, error: "TsqlError"):
        super().__init__(f"error {error.number} ended the procedure")
        self.error = error


class UnsupportedRaise(SupportLibraryError):
    """A RAISERROR option that changes what the server does outside the session and that this library does not do."""


class UnknownTable(SupportLibraryError):
    """A write named a table the catalog description does not hold."""


class ManualWrite(SupportLibraryError):
    """A write the wrapper cannot do exactly: the code that makes it is manual work."""


class UnsupportedCollation(SupportLibraryError):
    """A comparison the library cannot reproduce exactly under this collation."""


class CounterNotInitialised(SupportLibraryError):
    """A table holds rows but its identity counter was never initialised: run the counter initialisation command."""

    def __init__(self, table: str):
        super().__init__(f"The identity counter of {table} was never initialised and the table holds rows. Run the "
                         f"counter initialisation command before any generated job writes to it.")
        self.table = table


class SettingNotSet(SupportLibraryError):
    """A named setting the client has not filled in yet."""


class UnknownSetting(SupportLibraryError):
    """A setting the settings file does not hold."""


class RestoreRefused(SupportLibraryError):
    """A rollback found a commit on a table that this job run did not make."""

    def __init__(self, table: str, version: int, foreign: list[int]):
        super().__init__(
            f"Cannot restore {table} to version {version}: the commits at versions {foreign} were not made by this job "
            f"run, and a restore would discard them. The transaction is marked for manual reconciliation in the state "
            f"table, and nothing was restored.")
        self.table, self.version, self.foreign = table, version, foreign


class VersionVacuumed(SupportLibraryError):
    """A rollback needs a table version whose files VACUUM has removed."""

    def __init__(self, table: str, version: int, detail: str = ""):
        super().__init__(
            f"Cannot restore {table} to version {version}: VACUUM removed files that version needs. The transaction is "
            f"marked for manual reconciliation in the state table, and nothing was restored. {detail}".strip())
        self.table, self.version = table, version


class RecoveryRefused(SupportLibraryError):
    """A forced recovery named a run that holds no live lease."""


class LeaseLost(SupportLibraryError):
    """The run's lease was taken away (a forced recovery, or an expiry while its heartbeat could not run)."""


def when(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class LeaseHeld(SupportLibraryError):
    """A second generated writer found a live lease."""

    def __init__(self, table: str | None, holder_job: str, holder_run: str, expires_at: float, now: float):
        what = f"table {table}" if table else "its tables"
        super().__init__(
            f"Cannot write {what}: the job '{holder_job}' (run '{holder_run}') holds a live write lease on it. The lease "
            f"expires at {when(expires_at)}, {max(0.0, expires_at - now):.0f} seconds from now, unless the holder's "
            f"heartbeat continues. Run generated jobs that share a table one after another.")
        self.table, self.holder_job, self.holder_run, self.expires_at = table, holder_job, holder_run, expires_at


class LeaseWaitTimeout(LeaseHeld):
    """A restarted run of the same job waited as long as it may for the earlier run's lease to expire."""

    def __init__(self, table: str | None, holder_job: str, holder_run: str, expires_at: float, now: float, waited: float):
        super().__init__(table, holder_job, holder_run, expires_at, now)
        self.args = (f"{self.args[0]} Waited {waited:.0f} seconds and gave up; raise the restart wait or the retry "
                     f"settings of the job.",)
