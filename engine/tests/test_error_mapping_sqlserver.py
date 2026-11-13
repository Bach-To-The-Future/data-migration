"""The SQL Server half of the error mapping (ADR 0019): each core error class to SQL Server's number, severity, state and
message, and whether the error ends the statement, the scope or the batch (spec section 6, "Errors"; ADR 0004).

Every entry is decided by the errors the corpus raises, catches or tests for, and tested against the sandbox SQL Server: the
same statement runs there, and the entry must give what it reported, how far it reached, and whether a CATCH at the same
level saw it."""
import json
import re

import pytest

from migrt.core import error_classes
from migrt.sources.sqlserver import error_mapping as half
from migrt_spark.source_errors import SourceErrors

import error_cases
from error_oracle import open_oracle


def mapping() -> SourceErrors:
    return SourceErrors.load(half.load())


# ---------------------------------------------------------------------- the file's shape (no server needed)

def test_the_half_has_an_entry_for_every_core_class_and_for_nothing_else():
    entries = set(half.load()["classes"])
    assert entries == set(error_classes.classes())


def test_every_entry_gives_a_number_a_severity_a_state_a_message_and_how_far_it_reaches():
    for name, entry in half.load()["classes"].items():
        for part in entry.get("variants", [entry]):
            assert set(part) >= {"number", "severity", "state", "message", "ends"}, name
            assert part["ends"] in ("statement", "scope", "batch"), name


def test_a_message_template_uses_only_the_details_its_class_declares():
    for name, entry in half.load()["classes"].items():
        declared = error_classes.require(name).detail_names
        for part in entry.get("variants", [entry]):
            for key in ("message", "fallback_message"):
                used = set(re.findall(r"\{(\w+)(?::\w+)?\}", part.get(key, "")))
                assert used <= declared, (name, key, used - declared)
            for field in ("number", "severity", "state"):
                if isinstance(part[field], dict):
                    assert part[field]["detail"] in declared, (name, field)


def test_the_reserved_number_is_one_the_mapping_never_gives_and_the_code_can_compare_against():
    data = half.load()
    reserved = data["reserved"]["number"]
    assert 50000 < reserved <= 2147483647                       # the range of a number the code may raise
    given = half.mapped_numbers()
    assert reserved not in given


def test_comparisons_against_a_number_the_mapping_does_not_cover_are_the_ones_flagged():
    assert half.covers(2627) and half.covers(8134) and half.covers(0)
    assert half.covers(50001) and half.covers(51000)            # the code's own numbers, raised by the code itself
    assert not half.covers(1205)                                # a deadlock victim: no class
    assert not half.covers(8152)                                # string truncation before 2019: no entry yet


def test_the_version_is_the_hash_of_the_content():
    assert re.fullmatch(r"[0-9a-f]{16}", half.version())


def test_the_half_is_valid_json_that_names_no_target():
    text = (half.PATH).read_text(encoding="utf-8")
    json.loads(text)
    assert not re.search(r"\bSpark\b|\bDelta\b|\bDatabricks\b", text)


# ---------------------------------------------------------------------- against SQL Server

@pytest.fixture(scope="module")
def server(support_database):
    sandbox = open_oracle(support_database)
    yield sandbox
    sandbox.oracle.run("WHILE @@TRANCOUNT > 0 ROLLBACK")
    sandbox.oracle.close()


def each_case(database="x"):
    return [pytest.param(c, id=c.id) for c in error_cases.cases(database)]


@pytest.mark.parametrize("case", each_case())
def test_the_entry_gives_what_sql_server_reports(server, case):
    case = next(c for c in error_cases.cases(server.database) if c.id == case.id)
    reported = server.error_of(case.tsql, case.cleanup)
    details = {k: server.plain(v) for k, v in case.details.items()}
    rendered = mapping().render(case.error_class, details)
    if case.unmapped_by_source:
        # SQL Server has a number of its own for it that the half does not cover: the entry must not claim another
        # error's number, and the comparison against the real number must be flagged.
        assert rendered.unmapped and rendered.number == half.reserved_number()
        assert reported.number != rendered.number and not half.covers(reported.number)
        return
    assert not rendered.unmapped, rendered.reason
    assert (rendered.number, rendered.severity, rendered.state) == (reported.number, reported.severity, reported.state)
    assert rendered.message == reported.text
    assert rendered.exact_message


@pytest.mark.parametrize("case", each_case())
def test_the_entry_says_how_far_sql_server_lets_the_error_reach(server, case):
    case = next(c for c in error_cases.cases(server.database) if c.id == case.id)
    rendered = mapping().render(case.error_class, {k: server.plain(v) for k, v in case.details.items()})
    assert server.reach(case) == case.ends, "the case's own expectation is wrong"
    if case.unmapped_by_source:
        return
    assert rendered.ends == case.ends or case.unmapped_by_source


@pytest.mark.parametrize("case", [pytest.param(c, id=c.id) for c in error_cases.cases("x") if c.error_class != "raised_error"])
def test_the_entry_says_whether_a_catch_at_the_same_level_sees_the_error(server, case):
    case = next(c for c in error_cases.cases(server.database) if c.id == case.id)
    rendered = mapping().render(case.error_class, {k: server.plain(v) for k, v in case.details.items()})
    caught = server.same_level_catches(case)
    assert caught == case.same_level_catchable, "the case's own expectation is wrong"
    if not case.unmapped_by_source:
        assert rendered.catchable_at_same_level == case.same_level_catchable


def test_a_name_that_fails_at_the_same_level_is_caught_one_call_deeper(server):
    """Finding 7.6 of the tested differences: the missing table is not caught beside the TRY, and is caught in a procedure
    the TRY calls."""
    proc = f"[{server.schema}].[Deeper]"
    server.oracle.run(f"CREATE PROCEDURE {proc} AS SELECT * FROM [{server.schema}].nosuch")
    seen = [m.text for m in server.messages(f"BEGIN TRY EXEC {proc}; END TRY BEGIN CATCH PRINT 'CAUGHT'; END CATCH;")]
    server.oracle.run(f"DROP PROCEDURE {proc}")
    assert "CAUGHT" in seen


def test_a_message_of_severity_ten_is_not_an_error_the_catch_sees(server):
    seen = [m.text for m in server.messages("BEGIN TRY RAISERROR('soft', 10, 1); END TRY BEGIN CATCH PRINT 'CAUGHT'; END CATCH;")]
    assert "CAUGHT" not in seen


def test_the_reserved_number_is_a_number_the_code_may_raise_and_the_server_accepts(server):
    reserved = half.load()["reserved"]["number"]
    reported = server.error_of(f"THROW {reserved}, 'unmapped', 1")
    assert reported.number == reserved
