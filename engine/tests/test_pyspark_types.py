"""The type mapping in its two halves (spec section 6, ADR 0019, issue #44): SQL Server types to vocabulary
types (the source half, `sources.sqlserver.types`), and vocabulary types to Spark types with their
representation rules (the target half, `targets.pyspark.types`). The expected values are the spec's table,
written out here independently of the code."""
import pytest

from migrt.core import vocabulary
from migrt.core.ir import VType
from migrt.sources.sqlserver import types as source
from migrt.targets.pyspark import types as target


def column(system_type, *, max_length=0, precision=0, scale=0, collation=None):
    return {"system_type": system_type, "max_length": max_length, "precision": precision, "scale": scale,
            "collation": collation}


# (SQL Server column, the Spark type, the representation rule or None, flagged)
SPEC_TABLE = [
    (column("bit"), "boolean", None, False),
    (column("tinyint", max_length=1), "smallint", "Range 0-255 enforced", False),
    (column("smallint", max_length=2), "smallint", None, False),
    (column("int", max_length=4), "int", None, False),
    (column("bigint", max_length=8), "bigint", None, False),
    (column("decimal", precision=12, scale=3), "decimal(12,3)", None, False),
    (column("numeric", precision=18, scale=0), "decimal(18,0)", None, False),
    (column("money", max_length=8), "decimal(19,4)", "Truncating arithmetic", False),
    (column("smallmoney", max_length=4), "decimal(10,4)", "Truncating arithmetic", False),
    (column("float", max_length=8), "double", None, False),
    (column("real", max_length=4), "float", None, False),
    (column("date"), "date", None, False),
    (column("datetime", scale=3), "timestamp_ntz",
     "Rounding to 1/300 second, reproduced by the write wrapper", False),
    (column("smalldatetime"), "timestamp_ntz", "Rounding to the minute, reproduced by the write wrapper", False),
    (column("datetime2", scale=7), "timestamp_ntz",
     "Keeps six fractional digits; the seventh is compared within a catalogued 100 ns tolerance, reported per "
     "artefact", False),
    (column("datetime2", scale=3), "timestamp_ntz", "Rounding to 3 fractional digits, reproduced by the write wrapper",
     False),
    (column("datetimeoffset", scale=7), "timestamp",
     "The offset is kept in a companion column, a target-table requirement listed in the report; seven fractional "
     "digits are kept as for a local timestamp", False),
    (column("time", scale=7), "string",
     "Fixed-width, zero-padded HH:MM:SS.fffffff, so text order is time order", True),
    (column("varchar", max_length=20, collation="SQL_Latin1_General_CP1_CI_AS"), "string",
     "Length enforced with SQL Server's truncation error, counted in bytes", False),
    (column("nvarchar", max_length=40, collation="SQL_Latin1_General_CP1_CI_AS"), "string",
     "Length enforced with SQL Server's truncation error, counted in UTF-16 units", False),
    (column("char", max_length=5, collation="SQL_Latin1_General_CP1_CI_AS"), "string",
     "Padded with spaces to the declared length; length enforced with SQL Server's truncation error, counted in "
     "bytes", False),
    (column("varchar", max_length=-1, collation="SQL_Latin1_General_CP1_CI_AS"), "string", None, False),
    (column("varbinary", max_length=16), "binary", None, False),
    (column("uniqueidentifier", max_length=16), "string",
     "Compared and ordered through the byte-order helper", False),
    (column("xml"), "string", None, True),
]


@pytest.mark.parametrize("declared, spark, rule, flagged", SPEC_TABLE)
def test_a_sql_server_type_reaches_its_spark_type_through_both_halves(declared, spark, rule, flagged):
    vocabulary_type = source.from_column(declared)
    found = target.mapping(vocabulary_type)

    assert found is not None
    assert (found.ddl, found.rule, found.flagged) == (spark, rule, flagged)


def test_a_datetimeoffset_column_needs_its_companion_offset_column():
    found = target.mapping(source.from_column(column("datetimeoffset", scale=7)))

    assert found is not None and found.companion_column == "offset"
    assert all(target.mapping(source.from_column(c)).companion_column is None  # type: ignore[union-attr]
               for c, *_ in SPEC_TABLE if c["system_type"] != "datetimeoffset")


@pytest.mark.parametrize("name", ["sql_variant", "hierarchyid", "geography", "geometry"])
def test_a_type_with_no_neutral_meaning_has_no_spark_type(name):
    assert target.mapping(source.from_column(column(name))) is None


def test_every_vocabulary_type_is_mapped_or_declared_not_a_column_type():
    """The mapping covers the whole vocabulary: a type added to Annex A without a decision here fails."""
    mapped = {t for t in vocabulary.types() if t not in target.NOT_COLUMN_TYPES}
    assert mapped == target.MAPPED_TYPES


def test_the_representation_rules_published_in_the_project_list_every_row_of_the_mapping():
    text = target.representation_rules_markdown()

    for _, spark, rule, _ in SPEC_TABLE:
        assert spark.split('(')[0].upper() in text.upper()
        if rule:
            assert rule.split(",")[0].split(";")[0] in text, rule
    assert "Compared by property, never by raw value" in text          # the row version row
    assert text == target.representation_rules_markdown()              # deterministic


def test_a_type_outside_the_mapping_is_not_silently_given_a_spark_type():
    assert target.mapping(VType.of("row_set")) is None
    assert target.mapping(VType.of("unknown")) is None
