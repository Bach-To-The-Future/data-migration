"""One table of error cases, used by both halves' tests (spec section 6, "Errors"; ADR 0019).

Each case is an error the corpus raises, catches or tests for, or that the write wrappers raise, or that Spark or Delta raise
for the same operation. It holds what SQL Server does (the statement, how far the error reaches, whether a CATCH at the same
level sees it) and, where a Spark or Delta failure stands for it, how to make that failure. The source half is checked against
SQL Server running `tsql`; the target half is checked against the failures `spark` makes; and the two together are checked
against each other: the failure, mapped to a class and then to the source's code, must equal what SQL Server reports.

Everything here is a test input. The expectations come from the oracle (SQL Server), never from the mapping files."""
from __future__ import annotations

import dataclasses
from typing import Any, Callable

STATEMENT, SCOPE, BATCH = "statement", "scope", "batch"

# The tables every case may use, in a schema {s} of the support database. Constraints are named so a message is stable.
SETUP = [
    "CREATE TABLE {s}.P (Id int IDENTITY(10, 2) NOT NULL CONSTRAINT PK_P PRIMARY KEY, Name varchar(10) NOT NULL "
    "CONSTRAINT UQ_P UNIQUE, Qty int NULL CONSTRAINT CK_P CHECK (Qty > 0))",
    "CREATE TABLE {s}.C (Id int IDENTITY(1, 1) NOT NULL CONSTRAINT PK_C PRIMARY KEY, PId int NULL CONSTRAINT FK_C "
    "FOREIGN KEY REFERENCES {s}.P (Id), Tag varchar(5) NULL)",
    "CREATE UNIQUE INDEX UX_C ON {s}.C (Tag) WHERE Tag IS NOT NULL",
    "CREATE TABLE {s}.K (Id int NOT NULL CONSTRAINT PK_K PRIMARY KEY, U varchar(5) NOT NULL CONSTRAINT UQ_K UNIQUE)",
    "CREATE TABLE {s}.TN (a int NOT NULL)",
    "INSERT {s}.P (Name, Qty) VALUES ('a', 1)",
    "INSERT {s}.C (PId, Tag) VALUES (10, 'x')",
    "INSERT {s}.K (Id, U) VALUES (1, 'a')",
    "CREATE PROCEDURE {s}.Recurse AS EXEC {s}.Recurse",
]

SPARK_SETUP = [
    "CREATE TABLE {s}.TN (a INT NOT NULL, b INT) USING DELTA",
    "CREATE TABLE {s}.CK (a INT, b INT) USING DELTA",
    "ALTER TABLE {s}.CK ADD CONSTRAINT CK_P CHECK (b > 0)",
    "INSERT INTO {s}.CK VALUES (1, 1)",
]


@dataclasses.dataclass(frozen=True)
class Case:
    id: str
    error_class: str
    tsql: str
    details: dict                                  # what a caller supplies for the message, as the source half renders it
    ends: str = STATEMENT                          # how far SQL Server lets the error reach
    same_level_catchable: bool = True              # False: a CATCH at the same level does not see it
    compile_time: bool = False                     # found when the batch is compiled: only the batch shape can be run
    # The failure that stands for it in Spark or Delta, when one does:
    spark: Callable[..., object] | None = None     # called with (spark, schema); raises the failure
    operation: str | None = None                   # the kind of operation, where it picks the class
    spark_details: dict | None = None              # what the target half must extract from that failure
    caller_details: dict = dataclasses.field(default_factory=dict)   # what the generated code adds (its types, its table)
    exact_message: bool = True                     # False: the failure does not carry what the message needs
    unmapped_by_source: bool = False               # the target maps it to this class, the source has no entry for it
    cleanup: str | None = None


def sql(statement: str) -> Callable[..., object]:
    def run(spark, schema):
        spark.sql(statement.replace("{s}", schema)).collect()
    return run


def delta_update(table: str, **assignments: str) -> Callable[..., object]:
    def run(spark, schema):
        from delta import DeltaTable

        values: dict[str, Any] = dict(assignments)
        DeltaTable.forName(spark, f"{schema}.{table}").update(set=values)
    return run


def cases(database: str) -> list[Case]:
    P = "{s}.P"
    return [
        Case("divide_by_zero", "divide_by_zero", "SELECT 1 / 0", {}, spark=sql("SELECT 1 / 0"), spark_details={}),
        Case("remainder_by_zero", "divide_by_zero", "SELECT 5 % 0", {}, spark=sql("SELECT 5 % 0"), spark_details={}),
        Case("integer_overflow", "arithmetic_overflow", "SELECT 2147483647 + 1",
             {"to_type": "integer(32, signed)"},
             spark=sql("SELECT CAST(2147483647 AS INT) + CAST(1 AS INT)"), spark_details={"to_type": "integer(32, signed)"}),
        Case("bigint_overflow", "arithmetic_overflow", "SELECT CAST(9223372036854775807 AS bigint) + 1",
             {"to_type": "integer(64, signed)"},
             spark=sql("SELECT 9223372036854775807 + 1"), spark_details={"to_type": "integer(64, signed)"}),
        Case("decimal_overflow", "arithmetic_overflow", "SELECT CAST(1000 AS decimal(3, 0))",
             {"to_type": "decimal", "from_type": "integer(32, signed)"},
             spark=sql("SELECT CAST(1000 AS DECIMAL(3, 0))"), spark_details={"to_type": "decimal"},
             caller_details={"from_type": "integer(32, signed)"}),
        # SQL Server has a number of its own for the smaller integers, which the source half does not cover yet: the failure
        # is flagged with the reserved number, never given another error's number.
        Case("smallint_overflow", "arithmetic_overflow", "SELECT CAST(70000 AS smallint)", {"to_type": "integer(16, signed)"},
             spark=sql("SELECT CAST(70000 AS SMALLINT)"), spark_details={"to_type": "integer(16, signed)"},
             unmapped_by_source=True),
        Case("text_to_int", "conversion_failure", "SELECT CAST('abc' AS int)",
             {"value": "abc", "to_type": "integer(32, signed)", "from_type": "string(bytes)"}, ends=BATCH,
             spark=sql("SELECT CAST('abc' AS INT)"), spark_details={"value": "abc", "to_type": "integer(32, signed)"},
             caller_details={"from_type": "string(bytes)"}),
        Case("text_to_bigint", "conversion_failure", "SELECT CAST('abc' AS bigint)",
             {"value": "abc", "to_type": "integer(64, signed)", "from_type": "string(bytes)"}, ends=BATCH,
             spark=sql("SELECT CAST('abc' AS BIGINT)"), spark_details={"value": "abc", "to_type": "integer(64, signed)"},
             caller_details={"from_type": "string(bytes)"}),
        Case("text_to_smallint", "conversion_failure", "SELECT CAST('abc' AS smallint)",
             {"value": "abc", "to_type": "integer(16, signed)", "from_type": "string(bytes)"}, ends=BATCH,
             spark=sql("SELECT CAST('abc' AS SMALLINT)"), spark_details={"value": "abc", "to_type": "integer(16, signed)"},
             caller_details={"from_type": "string(bytes)"}),
        Case("national_text_to_int", "conversion_failure", "SELECT CAST(N'abc' AS int)",
             {"value": "abc", "to_type": "integer(32, signed)", "from_type": "string(utf16)"}, ends=BATCH,
             spark=sql("SELECT CAST('abc' AS INT)"), spark_details={"value": "abc", "to_type": "integer(32, signed)"},
             caller_details={"from_type": "string(utf16)"}),
        Case("text_to_decimal", "conversion_failure", "SELECT CAST('abc' AS decimal(5, 2))",
             {"value": "abc", "to_type": "decimal", "from_type": "string(bytes)"}, ends=BATCH,
             spark=sql("SELECT CAST('abc' AS DECIMAL(5, 2))"), spark_details={"value": "abc", "to_type": "decimal"},
             caller_details={"from_type": "string(bytes)"}),
        Case("text_to_int_without_the_callers_type", "conversion_failure", "SELECT CAST('abc' AS int)",
             {"value": "abc", "to_type": "integer(32, signed)", "from_type": "string(bytes)"}, ends=BATCH,
             spark=sql("SELECT CAST('abc' AS INT)"), spark_details={"value": "abc", "to_type": "integer(32, signed)"},
             exact_message=False),
        Case("text_to_datetime", "datetime_conversion_failure", "SELECT CAST('x' AS datetime)", {"value": "x"}, ends=BATCH,
             spark=sql("SELECT CAST('x' AS TIMESTAMP_NTZ)"), spark_details={"value": "x"}),
        Case("text_to_date", "datetime_conversion_failure", "SELECT CAST('x' AS date)", {"value": "x"}, ends=BATCH,
             spark=sql("SELECT CAST('x' AS DATE)"), spark_details={"value": "x"}),
        Case("subquery_returns_two_rows", "scalar_subquery_multiple_rows",
             "SELECT (SELECT a FROM (VALUES (1), (2)) AS v (a))", {},
             spark=sql("SELECT (SELECT a FROM (VALUES (1), (2)) AS v (a))"), spark_details={}),
        Case("null_into_not_null", "not_null_violation", "INSERT {s}.TN VALUES (NULL)",
             {"column": "a", "table": f"{database}.{{s}}.TN", "verb": "INSERT"},
             spark=sql("INSERT INTO {s}.TN VALUES (NULL, 1)"), spark_details={"column": "a"},
             caller_details={"table": f"{database}.{{s}}.TN", "verb": "INSERT"}),
        Case("check_constraint", "check_violation", f"INSERT {P} (Name, Qty) VALUES ('z', 0)",
             {"constraint": "CK_P", "verb": "INSERT", "database": database, "table": "{s}.P", "column": "Qty"},
             spark=sql("INSERT INTO {s}.CK VALUES (1, 0)"), spark_details={"constraint": "ck_p"},     # Delta folds the name to lower case
             caller_details={"constraint": "CK_P", "verb": "INSERT", "database": database, "table": "{s}.P", "column": "Qty"}),
        Case("check_constraint_on_update", "check_violation", f"UPDATE {P} SET Qty = 0",
             {"constraint": "CK_P", "verb": "UPDATE", "database": database, "table": "{s}.P", "column": "Qty"},
             spark=delta_update("CK", b="0"), spark_details={"constraint": "ck_p"},
             caller_details={"constraint": "CK_P", "verb": "UPDATE", "database": database, "table": "{s}.P", "column": "Qty"}),
        Case("foreign_key_on_insert", "foreign_key_violation", "INSERT {s}.C (PId, Tag) VALUES (99, 'y')",
             {"constraint": "FK_C", "kind": "FOREIGN KEY", "verb": "INSERT", "database": database, "table": "{s}.P",
              "column": "Id"}),
        Case("foreign_key_on_delete", "foreign_key_violation", f"DELETE {P}",
             {"constraint": "FK_C", "kind": "REFERENCE", "verb": "DELETE", "database": database, "table": "{s}.C",
              "column": "PId"}),
        Case("primary_key", "primary_key_violation", "INSERT {s}.K (Id, U) VALUES (1, 'b')",
             {"constraint": "PK_K", "table": "{s}.K", "key": "(1)"}),
        Case("unique_constraint", "unique_constraint_violation", "INSERT {s}.K (Id, U) VALUES (2, 'a')",
             {"constraint": "UQ_K", "table": "{s}.K", "key": "(a)"}),
        Case("unique_index", "unique_index_violation", "INSERT {s}.C (PId, Tag) VALUES (10, 'x')",
             {"index": "UX_C", "table": "{s}.C", "key": "(x)"}),
        Case("missing_table", "object_not_found", "SELECT * FROM {s}.nosuch", {"name": "{s}.nosuch"}, ends=SCOPE,
             same_level_catchable=False, spark=sql("SELECT * FROM {s}.nosuch"), spark_details={"name": "{s}.nosuch"}),
        Case("missing_column", "column_not_found", "SELECT zzz FROM {s}.TN", {"name": "zzz"}, ends=SCOPE,
             same_level_catchable=False, spark=sql("SELECT zzz FROM {s}.TN"), spark_details={"name": "zzz"}),
        Case("missing_qualified_column", "column_not_found", "SELECT t.zzz FROM {s}.TN AS t", {"name": "zzz"}, ends=SCOPE,
             same_level_catchable=False, spark=sql("SELECT t.zzz FROM {s}.TN AS t"), spark_details={"name": "zzz"}),
        Case("table_exists", "object_already_exists", "CREATE TABLE {s}.TN (a int)", {"name": "TN"}, ends=BATCH,
             spark=sql("CREATE TABLE {s}.TN (a INT) USING DELTA"), spark_details={"name": "TN"}),
        Case("drop_missing", "drop_target_not_found", "DROP TABLE {s}.nosuch", {"name": "{s}.nosuch"},
             spark=sql("DROP TABLE {s}.nosuch"), operation="drop", spark_details={"name": "{s}.nosuch"}),
        Case("identity_insert", "identity_insert_not_allowed", f"INSERT {P} (Id, Name) VALUES (5, 'q')", {"table": "P"}),
        Case("identity_update", "identity_update_not_allowed", f"UPDATE {P} SET Id = 3", {"column": "Id"}, ends=BATCH,
             same_level_catchable=False, compile_time=True),
        Case("nesting", "nesting_level_exceeded", "EXEC {s}.Recurse", {}, ends=BATCH),
        Case("commit_without_begin", "transaction_request_without_begin", "COMMIT TRANSACTION", {"request": "COMMIT"}),
        Case("rollback_without_begin", "transaction_request_without_begin", "ROLLBACK TRANSACTION", {"request": "ROLLBACK"}),
        Case("save_without_begin", "transaction_request_without_begin", "SAVE TRANSACTION s1", {"request": "SAVE"}, ends=BATCH),
        Case("rollback_to_unknown_savepoint", "savepoint_not_found", "BEGIN TRAN; ROLLBACK TRAN nope", {"name": "nope"},
             cleanup="WHILE @@TRANCOUNT > 0 ROLLBACK"),
        Case("raiserror_16", "raised_error", "RAISERROR('boom %s', 16, 3, 'x')",
             {"message": "boom x", "severity": 16, "state": 3}),
        Case("raiserror_11", "raised_error", "RAISERROR('soft', 11, 1)", {"message": "soft", "severity": 11, "state": 1}),
        Case("throw", "thrown_error", "THROW 50001, 'boom', 7", {"number": 50001, "message": "boom", "state": 7}, ends=BATCH),
        Case("throw_below_range", "throw_number_out_of_range", "THROW 49999, 'x', 1", {"number": 49999}, ends=BATCH),
        Case("raiserror_system_severity", "raise_severity_not_permitted", "RAISERROR('s', 19, 1)", {}),
        Case("raiserror_bad_format", "raise_format_invalid", "RAISERROR('percent %', 16, 1)", {"format": "%"}),
        Case("raiserror_wrong_type", "raise_argument_mismatch", "RAISERROR('%d', 16, 1, 'x')", {"index": 1}),
    ]
