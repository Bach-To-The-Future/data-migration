"""The core's neutral error classes (ADR 0019; spec section 6, "Errors"): the one list a target maps its failures to and a
source maps to its own codes. These tests need no server."""
import ast
import pathlib
import re

import pytest

from migrt.core import error_classes

CORE = pathlib.Path(error_classes.__file__).parent


def test_every_class_has_a_snake_case_name_a_meaning_and_declared_details():
    classes = error_classes.classes()
    assert classes, "the list of classes is empty"
    for name, cls in classes.items():
        assert re.fullmatch(r"[a-z][a-z0-9_]*", name), name
        assert cls.name == name
        assert len(cls.meaning) > 20, f"{name} needs a meaning a person can read"
        detail_names = [d.name for d in cls.details]
        assert len(detail_names) == len(set(detail_names)), f"{name} declares a detail twice"
        for d in cls.details:
            assert re.fullmatch(r"[a-z][a-z0-9_]*", d.name), (name, d.name)
            assert d.meaning


def test_the_list_holds_the_errors_the_corpus_raises_catches_or_tests_for_and_the_ones_the_library_raises():
    # ERROR_NUMBER() NOT IN (2601, 2627), THROW 50001, RAISERROR at 16, 11 and 10, @@ERROR after a duplicate key; the
    # write wrappers' constraint errors; the arithmetic and conversion failures ANSI mode makes Spark raise.
    expected = {"divide_by_zero", "arithmetic_overflow", "conversion_failure", "datetime_conversion_failure",
                "scalar_subquery_multiple_rows", "not_null_violation", "check_violation", "foreign_key_violation",
                "primary_key_violation", "unique_constraint_violation", "unique_index_violation", "object_not_found",
                "column_not_found", "object_already_exists", "drop_target_not_found", "identity_insert_not_allowed",
                "identity_update_not_allowed", "nesting_level_exceeded", "transaction_request_without_begin",
                "savepoint_not_found", "transaction_count_mismatch", "raised_error", "thrown_error",
                "throw_number_out_of_range", "raise_severity_not_permitted", "raise_format_invalid", "raise_argument_mismatch"}
    assert expected <= set(error_classes.classes())


def test_a_class_names_a_failure_not_a_number_or_a_runtime():
    """The list is neutral: no class is named for a SQL Server number, a Spark condition or an Oracle code."""
    for name in error_classes.classes():
        assert not re.search(r"\d{3,}", name), name
        assert not re.search(r"spark|delta|tsql|sqlserver|ora_|sqlstate", name), name


def test_the_text_of_a_class_names_no_runtime_either():
    for cls in error_classes.classes().values():
        text = cls.meaning + " ".join(d.meaning for d in cls.details)
        assert not re.search(r"\bSpark\b|\bDelta\b|\bSQL Server\b|\bT-SQL\b|\bOracle\b", text), cls.name


def test_the_version_is_the_hash_of_the_content_and_changes_with_it():
    assert re.fullmatch(r"[0-9a-f]{16}", error_classes.version())
    assert error_classes.version() == error_classes.version()
    changed = {**error_classes.classes()}
    changed.pop("divide_by_zero")
    assert error_classes.version_of(changed) != error_classes.version()


def test_a_name_outside_the_list_is_refused_loudly():
    with pytest.raises(error_classes.UnknownErrorClass):
        error_classes.require("no_such_failure")
    assert error_classes.require("divide_by_zero").name == "divide_by_zero"


def test_the_core_list_imports_nothing_from_a_source_or_a_target():
    tree = ast.parse((CORE / "error_classes.py").read_text(encoding="utf-8"))
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not {i for i in imported if "sources" in i or "targets" in i}
