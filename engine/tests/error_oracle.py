"""SQL Server as the oracle for the error mapping: the same statement, run in three shapes, says what it reports, how far it
reaches, and whether a CATCH beside it sees it. Shared by both halves' tests."""
from __future__ import annotations

import os
import uuid

from conftest import RUN
from support_harness import Oracle

import error_cases


class ErrorOracle:
    def __init__(self, oracle: Oracle, schema: str, database: str):
        self.oracle, self.schema, self.database = oracle, schema, database
        self.counter = 0

    def sub(self, text: str) -> str:
        return text.replace("{s}", f"[{self.schema}]")

    def plain(self, value):
        """A detail value, with the schema of this run where the case wrote {s}."""
        if isinstance(value, str):
            return value.replace("{s}", self.schema)
        return value

    def messages(self, batch: str, cleanup: str | None = None) -> list:
        self.oracle.run(self.sub(batch))
        found = list(self.oracle._messages)
        self.oracle.run("WHILE @@TRANCOUNT > 0 ROLLBACK" if cleanup is None else cleanup)
        return found

    def error_of(self, batch: str, cleanup: str | None = None):
        found = [m for m in self.messages(batch, cleanup) if m.severity > 10]
        assert found, f"SQL Server raised no error for {batch}"
        return found[0]

    def reach(self, case: error_cases.Case) -> str:
        """How far the error reaches: the next statement of the batch runs (statement); the rest of the procedure does
        not but its caller goes on (scope); neither does (batch)."""
        statement = case.tsql
        in_batch = [m.text for m in self.messages(f"{statement};\nPRINT 'NEXT';", case.cleanup) if m.severity <= 10]
        if "NEXT" in in_batch:
            return error_cases.STATEMENT
        if case.compile_time:
            return error_cases.BATCH
        self.counter += 1
        proc = f"[{self.schema}].[Reach{self.counter}]"
        self.oracle.run(f"CREATE PROCEDURE {proc} AS BEGIN {self.sub(statement)}; PRINT 'INSIDE'; END")
        seen = [m.text for m in self.messages(f"EXEC {proc}; PRINT 'CALLER';", case.cleanup)]
        self.oracle.run(f"DROP PROCEDURE {proc}")
        return error_cases.SCOPE if "CALLER" in seen else error_cases.BATCH

    def same_level_catches(self, case: error_cases.Case) -> bool:
        statement = case.tsql
        seen = [m.text for m in self.messages(
            f"BEGIN TRY {statement}; END TRY BEGIN CATCH PRINT 'CAUGHT'; END CATCH;", case.cleanup)]
        return "CAUGHT" in seen




def open_oracle(database: str) -> ErrorOracle:
    oracle = Oracle(os.environ.get("MSSQL_HOST", "sqlserver"), 1433, "sa", os.environ["MSSQL_SA_PASSWORD"], database)
    schema = f"e{RUN}_{uuid.uuid4().hex[:4]}"
    oracle.run(f"IF SCHEMA_ID('{schema}') IS NULL EXEC('CREATE SCHEMA [{schema}]')")
    for statement in error_cases.SETUP:
        _, errors = oracle.run(statement.replace("{s}", f"[{schema}]"))
        assert not errors, (statement, errors)
    return ErrorOracle(oracle, schema, database)
