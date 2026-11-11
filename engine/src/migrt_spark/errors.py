"""The errors of the support library.

`TsqlError` is an error SQL Server would raise, with its number, severity, state and text. The rest are the library's own
named errors: each says what went wrong and what to do, and none is ever swallowed (CODING_STANDARDS.md: nothing
degrades silently)."""
from __future__ import annotations

import datetime


class TsqlError(Exception):
    """An error as SQL Server reports it."""

    def __init__(self, number: int, severity: int, state: int, message: str):
        super().__init__(message)
        self.number, self.severity, self.state, self.message = number, severity, state, message


class SupportLibraryError(Exception):
    """The base of the library's own named errors."""


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
                         f"counter initialisation command before any converted job writes to it.")
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
    """A second converted writer found a live lease."""

    def __init__(self, table: str | None, holder_job: str, holder_run: str, expires_at: float, now: float):
        what = f"table {table}" if table else "its tables"
        super().__init__(
            f"Cannot write {what}: the job '{holder_job}' (run '{holder_run}') holds a live write lease on it. The lease "
            f"expires at {when(expires_at)}, {max(0.0, expires_at - now):.0f} seconds from now, unless the holder's "
            f"heartbeat continues. Run converted jobs that share a table one after another.")
        self.table, self.holder_job, self.holder_run, self.expires_at = table, holder_job, holder_run, expires_at


class LeaseWaitTimeout(LeaseHeld):
    """A restarted run of the same job waited as long as it may for the earlier run's lease to expire."""

    def __init__(self, table: str | None, holder_job: str, holder_run: str, expires_at: float, now: float, waited: float):
        super().__init__(table, holder_job, holder_run, expires_at, now)
        self.args = (f"{self.args[0]} Waited {waited:.0f} seconds and gave up; raise the restart wait or the retry "
                     f"settings of the job.",)
