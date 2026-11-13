"""Source comparisons against error numbers (spec section 6, "Errors"): converted code can raise only the numbers the error
mapping gives, so a comparison of `ERROR_NUMBER()` or `@@ERROR` against any other number is flagged in the report. The numbers
the program raises itself (50000 and above) and zero are never flagged."""
import json

import pytest

from migrt.sources.sqlserver import error_comparisons, parser


def parsed(sql: str):
    return parser.parse("test", sql, 160, True)


def numbers(sql: str) -> list[int]:
    return [c.number for c in error_comparisons.find(parsed(sql))]


@pytest.mark.parametrize("sql, expected", [
    ("IF ERROR_NUMBER() = 2627 PRINT 'dup'", [2627]),
    ("IF 2627 = ERROR_NUMBER() PRINT 'dup'", [2627]),
    ("IF ERROR_NUMBER() NOT IN (2601, 2627) THROW;", [2601, 2627]),
    ("IF ERROR_NUMBER() <> 1205 THROW;", [1205]),
    ("IF @@ERROR = 547 PRINT 'fk'", [547]),
    ("IF @@ERROR <> 0 PRINT 'failed'", [0]),
    ("DECLARE @err int; SELECT @err = @@ERROR; IF @err = 8152 PRINT 'x'", [8152]),
    ("DECLARE @e int; SET @e = ERROR_NUMBER(); IF @e IN (1205, 8152) PRINT 'x'", [1205, 8152]),
    ("DECLARE @e int = @@ERROR; IF @e = 515 PRINT 'x'", [515]),
    ("SELECT CASE ERROR_NUMBER() WHEN 1205 THEN 'deadlock' WHEN 8134 THEN 'zero' ELSE 'other' END", [1205, 8134]),
    ("SELECT CASE WHEN ERROR_NUMBER() = 1205 THEN 1 ELSE 0 END", [1205]),
    ("DECLARE @x int = 5; IF @x = 1205 PRINT 'not an error number'", []),
    ("IF ERROR_SEVERITY() = 16 PRINT 'severity, not number'", []),
    ("IF ERROR_NUMBER() = @n PRINT 'no literal'", []),
    ("IF ERROR_NUMBER() = -1 PRINT 'negative'", [-1]),
], ids=lambda v: v if isinstance(v, str) and len(v) < 30 else None)
def test_every_comparison_of_an_error_number_with_a_whole_number_is_found(sql, expected):
    assert sorted(numbers(sql)) == sorted(expected)


def test_a_comparison_carries_its_position_and_its_text():
    [c] = error_comparisons.find(parsed("PRINT 'x';\nIF ERROR_NUMBER() = 1205 PRINT 'deadlock'"))
    assert (c.number, c.position.start_line) == (1205, 2)
    assert "1205" in c.text


def test_only_the_numbers_the_mapping_does_not_cover_are_flagged():
    found = error_comparisons.find(parsed("IF ERROR_NUMBER() IN (0, 2601, 2627, 1205, 8152, 50001, 51000) PRINT 'x'"))
    assert sorted(c.number for c in error_comparisons.flagged(found)) == [1205, 8152]


def test_a_flag_says_what_will_happen_and_what_to_do_in_the_sources_words():
    [c] = error_comparisons.flagged(error_comparisons.find(parsed("IF ERROR_NUMBER() = 1205 PRINT 'x'")))
    flag = error_comparisons.flag(c)
    assert flag["kind"] == "unmapped_error_number" and flag["number"] == 1205
    assert "1205" in flag["text"] and "reserved" in flag["text"]
    assert flag["source_position"].startswith("L1")
    json.dumps(flag)                                                       # the report data is JSON


def test_a_preview_run_lists_the_flag_at_the_artefact(engagement, scratch_database, tmp_path):
    """The flag reaches report-data.json: the procedure compares the number of the error it caught with a deadlock's."""
    from conftest import sa_connection, sandbox_server
    from migrt import engine

    with sa_connection(scratch_database) as conn:
        conn.query("""CREATE OR ALTER PROCEDURE s.uspRetry AS
            BEGIN
                BEGIN TRY
                    EXEC s.uspLog;
                END TRY
                BEGIN CATCH
                    IF ERROR_NUMBER() IN (1205, 2627) THROW 50010, 'try again', 1;
                END CATCH
            END""")
    server = sandbox_server(databases=(scratch_database,))
    result = engine.preview_run(engagement, server, artefacts=[f"{scratch_database}.s.uspRetry"], out_dir=tmp_path / "out")
    data = json.loads(result.report_data_path.read_text(encoding="utf-8"))
    [item] = data["artefacts"]
    assert [f["number"] for f in item["flags"] if f["kind"] == "unmapped_error_number"] == [1205]
    assert all("converted" not in f["text"].lower() for f in item["flags"])
