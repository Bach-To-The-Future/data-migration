"""The two halves of the error mapping, together (ADR 0019; spec section 6, "Errors"): a Spark or Delta failure goes to a
neutral class by the target's half, and the class goes to SQL Server's error by the source's half. The result must be what
SQL Server reports for the same operation. The oracle is SQL Server; nothing here is read from the mapping files."""
import pytest

from migrt.core import error_classes
from migrt_spark import target_errors
from migrt_spark.source_errors import SourceErrors
from migrt_spark.target_errors import SparkFailures

import error_cases
from error_oracle import open_oracle
from migrt.sources.sqlserver import error_mapping as half


@pytest.fixture(scope="module")
def server(support_database):
    sandbox = open_oracle(support_database)
    yield sandbox
    sandbox.oracle.run("WHILE @@TRANCOUNT > 0 ROLLBACK")
    sandbox.oracle.close()


@pytest.fixture(scope="module")
def schema(spark, server):
    """A Spark schema with the tables the cases fail on, named like the SQL Server one."""
    name = server.schema
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {name}")
    for statement in error_cases.SPARK_SETUP:
        spark.sql(statement.replace("{s}", name))
    yield name
    spark.sql(f"DROP SCHEMA IF EXISTS {name} CASCADE")


def spark_cases():
    return [pytest.param(c, id=c.id) for c in error_cases.cases("x") if c.spark is not None]


def resolved(server, values):
    return {k: server.plain(v) for k, v in values.items()}


def failure(case, spark, schema) -> BaseException:
    try:
        case.spark(spark, schema)
    except BaseException as exc:                                       # noqa: BLE001 - the failure is the subject
        return exc
    raise AssertionError(f"{case.id}: Spark did not fail")


@pytest.mark.parametrize("case", spark_cases())
def test_a_spark_or_delta_failure_goes_to_the_class_and_details_the_case_names(spark, schema, server, case):
    case = next(c for c in error_cases.cases(server.database) if c.id == case.id)
    classified = SparkFailures.load().classify(failure(case, spark, schema), case.operation)
    assert classified is not None and classified.error_class == case.error_class, classified
    assert classified.details == resolved(server, case.spark_details or {})
    assert error_classes.require(case.error_class).detail_names >= set(classified.details)


@pytest.mark.parametrize("case", spark_cases())
def test_the_failure_mapped_through_both_halves_is_what_sql_server_reports(spark, schema, server, case):
    case = next(c for c in error_cases.cases(server.database) if c.id == case.id)
    classified = SparkFailures.load().classify(failure(case, spark, schema), case.operation)
    assert classified is not None and classified.error_class is not None
    details = {**classified.details, **resolved(server, case.caller_details)}
    rendered = SourceErrors.load(half.load()).render(classified.error_class, details)
    reported = server.error_of(case.tsql, case.cleanup)
    if case.unmapped_by_source:
        assert rendered.unmapped and rendered.number == half.reserved_number()
        assert not half.covers(reported.number)
        return
    assert not rendered.unmapped, rendered.reason
    assert (rendered.number, rendered.severity, rendered.state) == (reported.number, reported.severity, reported.state)
    assert rendered.exact_message is case.exact_message
    if case.exact_message:
        assert rendered.message == reported.text
    else:
        assert rendered.message != reported.text


def raised_by(run) -> BaseException:
    try:
        run()
    except BaseException as exc:                                       # noqa: BLE001 - the failure is the subject
        return exc
    raise AssertionError("it did not fail")


def test_a_failure_no_rule_covers_has_no_class_and_says_why(spark, schema):
    for run in (lambda: spark.sql("SELECT * FROM parquet.`/nonexistent/path`").collect(),
                lambda: spark.sql("SELECT assert_true(false)").collect()):
        classified = SparkFailures.load().classify(raised_by(run))
        assert classified is not None and classified.error_class is None
        assert classified.condition and classified.reason and classified.condition in classified.reason


def test_a_drop_that_finds_nothing_and_a_read_that_finds_nothing_are_different_classes(spark, schema):
    exc = raised_by(lambda: spark.sql(f"SELECT * FROM {schema}.nope").collect())
    read, drop = SparkFailures.load().classify(exc), SparkFailures.load().classify(exc, "drop")
    assert read is not None and drop is not None
    assert (read.error_class, drop.error_class) == ("object_not_found", "drop_target_not_found")


def test_a_python_error_is_not_a_failure_of_the_runtime():
    """A bug in the generated code, or in the library, is never an error the program could have caught."""
    for bug in (TypeError("x"), KeyError("k"), ZeroDivisionError("y"), RuntimeError("z")):
        assert SparkFailures.load().classify(bug) is None


def test_a_class_the_source_does_not_list_is_unmapped_and_so_is_one_without_a_variant():
    source = SourceErrors.load(half.load())
    reserved = half.reserved_number()
    assert source.render("not_a_class").number == reserved
    assert source.render("arithmetic_overflow", {"to_type": "float(53)"}).number == reserved
    assert source.render("arithmetic_overflow", {"to_type": "float(53)"}).unmapped
    assert source.render("arithmetic_overflow", {}).unmapped


def test_every_rule_names_a_class_of_the_core_and_only_its_details():
    for rule in SparkFailures.load().rules:
        assert set(rule["details"]) <= error_classes.require(rule["class"]).detail_names, rule


def test_the_target_half_names_no_source():
    text = target_errors.PATH.read_text(encoding="utf-8")
    assert not any(word in text for word in ("SQL Server", "T-SQL", "Oracle", "tsql"))
