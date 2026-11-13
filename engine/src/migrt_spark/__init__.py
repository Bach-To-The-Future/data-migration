"""migrt_spark: the support library of the PySpark target (spec section 7).

Generated code imports it to reproduce what SQL Server does and Spark does not: session state with its write wrappers,
transactions, constraint and foreign-key enforcement, triggers, identity and row-version counters, write leases and
recovery on start."""
from .config import VERSION, Config
from .errors import (CounterNotInitialised, ErrorFieldUnknown, ErrorMappingNotLoaded, Failure, LeaseHeld, LeaseLost,
                     LeaseWaitTimeout, ManualWrite, MissingDetail, RecoveryRefused, RestoreRefused, RethrowOutsideHandler,
                     ScopeAborted, SettingNotSet, TsqlError, UnknownSetting, UnknownTable, UnsupportedCollation,
                     UnsupportedRaise, VersionVacuumed)
from .recovery import ForcedRecovery, Report, forced_recoveries, recover
from .session import Guard, Message, SessionState, TryBlock
from .source_errors import Rendered, SourceErrors
from .state import create_state_table, state_table_ddl
from .target_errors import Classified, SparkFailures
from .tables import Check, Column, ForeignKey, TableSpec, Trigger, Unique
from .transactions import Transaction

__all__ = [
    "VERSION", "Config", "SessionState", "Transaction", "TableSpec", "Column", "Unique", "Check", "ForeignKey", "Trigger",
    "TsqlError", "UnknownTable", "ManualWrite", "UnsupportedCollation", "CounterNotInitialised", "SettingNotSet",
    "UnknownSetting", "RestoreRefused", "VersionVacuumed", "RecoveryRefused", "LeaseHeld", "LeaseWaitTimeout", "LeaseLost",
    "Failure", "ErrorMappingNotLoaded", "MissingDetail", "ErrorFieldUnknown", "RethrowOutsideHandler", "ScopeAborted",
    "UnsupportedRaise", "TryBlock", "Guard", "Message", "SourceErrors", "Rendered", "SparkFailures", "Classified",
    "recover", "forced_recoveries", "Report", "ForcedRecovery", "create_state_table", "state_table_ddl",
]
