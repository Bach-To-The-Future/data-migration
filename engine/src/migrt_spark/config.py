"""The library's version and the settings of a session that are not the project's settings file."""
from __future__ import annotations

import dataclasses
import time
from typing import Callable

VERSION = "0.1.0"
VERSION_TAG = "v" + VERSION.replace(".", "_")
STATE_SCHEMA_VERSION = 1
LEASE_EXPIRY_SECONDS = 600.0
TRIGGER_NESTING_LIMIT = 32


@dataclasses.dataclass(frozen=True)
class Config:
    """How a session behaves. The defaults are SQL Server's, and the lease expiry is the specification's (ten minutes)."""
    lease_expiry_seconds: float = LEASE_EXPIRY_SECONDS
    heartbeat_seconds: float | None = None               # default: a tenth of the expiry
    poll_seconds: float = 5.0                            # how often a restarted run looks again
    restart_wait_timeout_seconds: float | None = None    # default: the expiry and two polls; 0 means do not wait
    nested_triggers: bool = True                         # the server option; a trigger's writes fire triggers
    recursive_triggers: bool = False                     # the database option RECURSIVE_TRIGGERS
    scratch_schema: str | None = None                    # default: the state table's schema
    database: str = "tempdb"                             # the database of temp tables and the row-version counter
    default_collation: str = "SQL_Latin1_General_CP1_CI_AS"
    clock: Callable[[], float] = time.time

    @property
    def heartbeat(self) -> float:
        return self.heartbeat_seconds if self.heartbeat_seconds is not None else self.lease_expiry_seconds / 10

    @property
    def restart_wait(self) -> float:
        if self.restart_wait_timeout_seconds is not None:
            return self.restart_wait_timeout_seconds
        return self.lease_expiry_seconds + 2 * self.poll_seconds
