"""The silent-differences catalogue as data (issue #102): the SQL Server source half and the
PySpark target half, checked against Annexes C and D and the operation vocabulary, and read
through the core's one interface."""
import pathlib
import re

import pytest

from migrt.core import differences, vocabulary
from migrt.sources.sqlserver import differences as source_differences
from migrt.targets.pyspark import differences as target_differences

PACKAGE = pathlib.Path(vocabulary.__file__).parents[1]
SPEC = PACKAGE.parents[2] / "docs" / "spec"
ROW = re.compile(r"\| (\d+\.\d+) \|")
BACKTICKED = re.compile(r"`([^`]+)`")


def annex_rows(name: str) -> dict[str, list[str]]:
    """Entry ID -> the cells of its row, in column order."""
    return {cells[0]: cells for _, cells in annex_table(name)}


def annex_sections(name: str) -> dict[str, str]:
    """Entry ID -> the heading of the section its row is in."""
    return {cells[0]: section for section, cells in annex_table(name)}


def annex_table(name: str):
    section = ""
    for line in (SPEC / name).read_text(encoding="utf-8").splitlines():
        if re.match(r"## \d+\. ", line):
            section = line[3:]
        elif ROW.match(line):
            yield section, [c.strip() for c in line.strip()[1:-1].split("|")]


ANNEX_C = annex_rows("annex-c-differences-sql-server.md")
ANNEX_D = annex_rows("annex-d-differences-pyspark.md")
SOURCE = source_differences.half()
TARGET = target_differences.half()


# ---------------------------------------------------------------- consistency with the annexes

def test_the_source_half_has_every_annex_c_entry():
    assert len(ANNEX_C) == 83
    assert SOURCE.ids() == tuple(ANNEX_C)


def test_the_target_half_has_every_annex_d_entry():
    assert len(ANNEX_D) == 84
    assert TARGET.ids() == tuple(ANNEX_D)


def test_the_only_id_in_one_half_is_the_documented_target_only_entry():
    assert set(SOURCE.ids()) - set(TARGET.ids()) == set()
    assert set(TARGET.ids()) - set(SOURCE.ids()) == {"9.14"}
    assert {e.id for e in TARGET.entries() if e.target_only} == {"9.14"}


@pytest.mark.parametrize("entry_id", list(ANNEX_C))
def test_a_source_entry_matches_its_annex_c_cells(entry_id):
    _, example, vocabulary_cell, behaviour, evidence = ANNEX_C[entry_id]
    entry = SOURCE.entry(entry_id)
    assert (entry.example, entry.vocabulary, entry.behaviour, entry.evidence) == (
        example, vocabulary_cell, behaviour, evidence)


@pytest.mark.parametrize("entry_id", list(ANNEX_D))
def test_a_target_entry_matches_its_annex_d_cells(entry_id):
    cells = ANNEX_D[entry_id]
    if len(cells) == 6:   # section 1: Spark with ANSI on, and with ANSI off
        _, example, behaviour, behaviour_ansi_off, reproduces, evidence = cells
    else:
        _, example, behaviour, reproduces, evidence = cells
        behaviour_ansi_off = None
    entry = TARGET.entry(entry_id)
    assert (entry.behaviour, entry.behaviour_ansi_off, entry.reproduces, entry.evidence) == (
        behaviour, behaviour_ansi_off, reproduces, evidence)
    # The example is the source construct's, held by the source half; only a target-only
    # entry holds its own.
    held = entry.example if entry.target_only else SOURCE.entry(entry_id).example
    assert held == example
    assert entry.target_only or entry.example is None


def test_each_entry_is_in_its_annex_section():
    assert {e.id: e.section for e in SOURCE.entries()} == annex_sections("annex-c-differences-sql-server.md")
    assert {e.id: e.section for e in TARGET.entries()} == annex_sections("annex-d-differences-pyspark.md")


@pytest.mark.parametrize("entry_id", [i for i in ANNEX_D if TARGET.entry(i).naive_built_in])
def test_a_naive_built_in_is_one_the_entrys_spark_cell_names(entry_id):
    entry = TARGET.entry(entry_id)
    cell = " ".join(filter(None, (entry.behaviour, entry.behaviour_ansi_off)))
    named = {name.split("(", 1)[0] for name in BACKTICKED.findall(cell)}
    assert entry.naive_built_in in named


# ---------------------------------------------------------------- consistency with the vocabulary

@pytest.mark.parametrize("entry_id", list(ANNEX_C))
def test_a_source_entry_names_only_operations_and_types_in_the_vocabulary(entry_id):
    entry = SOURCE.entry(entry_id)
    assert entry.operations or entry.types
    assert set(entry.operations) <= vocabulary.operations()
    assert set(entry.types) <= vocabulary.types()
    assert not set(entry.operations) & set(entry.types)


@pytest.mark.parametrize("entry_id", list(ANNEX_C))
def test_every_name_in_a_vocabulary_cell_is_an_operation_a_type_or_an_operations_argument(entry_id):
    entry = SOURCE.entry(entry_id)
    named = BACKTICKED.findall(entry.vocabulary)
    arguments = {a for args in entry.arguments.values() for a in args}
    assert set(named) == set(entry.operations) | set(entry.types) | arguments
    for operation, args in entry.arguments.items():
        assert operation in entry.operations
        inside = re.search(rf"`{operation}` \(([^)]*)\)", entry.vocabulary)
        assert inside and set(args) <= set(BACKTICKED.findall(inside.group(1)))


def test_an_argument_is_never_an_operation_or_a_type():
    for entry in SOURCE.entries():
        for args in entry.arguments.values():
            assert not set(args) & (vocabulary.operations() | vocabulary.types()), entry.id


# ---------------------------------------------------------------- the one interface

def catalogue() -> differences.Catalogue:
    return differences.Catalogue(SOURCE, TARGET)


def test_entries_are_found_by_vocabulary_operation():
    found = catalogue().for_operation("sum")
    assert [source.id for source, _ in found] == ["3.4", "3.13", "3.14"]
    assert all(target is not None and target.id == source.id for source, target in found)


def test_entries_are_found_by_vocabulary_type_including_those_naming_no_operation():
    found = catalogue().for_type("local_timestamp")
    assert [source.id for source, _ in found] == ["4.1", "4.2", "4.3", "4.5"]
    with pytest.raises(ValueError, match="not_a_type"):
        catalogue().for_type("not_a_type")


def test_every_entry_is_reachable_by_an_operation_or_a_type():
    reached = {s.id for op in vocabulary.operations() for s, _ in catalogue().for_operation(op)}
    reached |= {s.id for ty in vocabulary.types() for s, _ in catalogue().for_type(ty)}
    assert reached == set(SOURCE.ids())


def test_an_operation_outside_the_vocabulary_is_refused_never_answered_with_nothing():
    with pytest.raises(ValueError, match="not_an_operation"):
        catalogue().for_operation("not_an_operation")


def test_an_unknown_entry_is_refused():
    with pytest.raises(KeyError, match="99.1"):
        SOURCE.entry("99.1")
    with pytest.raises(KeyError, match="99.1"):
        TARGET.entry("99.1")


def test_a_difference_is_worded_from_the_source_half_and_the_target_half():
    text = catalogue().difference("3.14")
    assert text == ("Silent difference 3.14: SQL Server raises an overflow error when a running total leaves "
                    "int, even if the final total fits, and whether it does can depend on the plan's "
                    "aggregation order; Spark raises only when the final total does not fit")


def test_an_entry_without_report_wording_is_refused_never_worded_empty():
    with pytest.raises(LookupError, match="1.1"):
        catalogue().difference("1.1")


def test_the_target_half_holds_no_sql_server_wording():
    for entry in TARGET.entries():
        if entry.wording:
            assert "SQL Server" not in entry.wording and "T-SQL" not in entry.wording, entry.id


# ---------------------------------------------------------------- versions

def test_each_half_has_a_version_derived_from_its_content():
    data = source_differences.data()
    same = differences.SourceHalf.from_data(data)
    changed = differences.SourceHalf.from_data({**data, "entries": data["entries"][:-1]})
    assert same.version == SOURCE.version
    assert changed.version != SOURCE.version
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", SOURCE.version)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", TARGET.version)


def test_the_catalogue_exposes_both_versions_for_the_fingerprint():
    assert catalogue().versions() == {"differences_source": SOURCE.version, "differences_target": TARGET.version}


def test_only_the_halves_own_modules_read_the_halves_data():
    readers = {p.relative_to(PACKAGE).as_posix() for p in PACKAGE.rglob("*.py")
               if "differences.json" in p.read_text(encoding="utf-8")}
    assert readers == {"sources/sqlserver/differences.py", "targets/pyspark/differences.py"}
