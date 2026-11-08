"""Name adjustments (spec section 3, "Name adjustments outlive snapshots"): a name the
target cannot take has each disallowed character replaced by `_`; names that then collide,
including names differing only by case, keep the first in sorted order and give the others
a numeric suffix; and a recorded adjustment never changes, so adding a name never renames
an existing one."""
import pytest

from migrt.core.name_adjustments import extend
from migrt.targets.pyspark import names


def test_unity_catalog_takes_most_names_unchanged():
    assert extend(names.UNITY_CATALOG, ["Order", "Order-Line", "Données"], {}) == {
        "Order": "Order", "Order-Line": "Order-Line", "Données": "Données"}


def test_each_character_unity_catalog_refuses_becomes_an_underscore():
    assert extend(names.UNITY_CATALOG, ["Order Line", "a.b/c", "tab\there\x7f"], {}) == {
        "Order Line": "Order_Line", "a.b/c": "a_b_c", "tab\there\x7f": "tab_here_"}


def test_names_differing_only_by_case_get_numeric_suffixes_in_sorted_order():
    adjusted = extend(names.UNITY_CATALOG, ["order", "Order", "ORDER"], {})
    assert adjusted == {"ORDER": "ORDER", "Order": "Order_2", "order": "order_3"}


def test_a_name_adjusted_into_another_collides_with_it():
    assert extend(names.UNITY_CATALOG, ["Order Line", "Order_Line"], {}) == {
        "Order Line": "Order_Line", "Order_Line": "Order_Line_2"}


def test_a_recorded_adjustment_is_kept_and_a_new_name_works_around_it():
    first = extend(names.UNITY_CATALOG, ["order"], {})
    later = extend(names.UNITY_CATALOG, ["Order", "order"], first)

    assert first == {"order": "order"}
    assert later == {"Order": "Order_2"}   # only the new name; "order" keeps its name


def test_python_modules_take_identifiers_only():
    adjusted = extend(names.PYTHON_MODULE, ["Order Line", "2024Totals", "class", "Données", "v"], {})
    assert adjusted == {"2024Totals": "_2024Totals", "Order Line": "Order_Line", "class": "class_2",
                        "Données": "Donn_es", "v": "v"}


def test_a_module_never_shadows_what_generated_code_imports():
    assert extend(names.PYTHON_MODULE, ["F", "DataFrame"], {}) == {"F": "F_2", "DataFrame": "DataFrame_2"}


def test_python_modules_collide_by_case_because_file_systems_may_ignore_it():
    assert extend(names.PYTHON_MODULE, ["vTotals", "VTOTALS"], {}) == {"VTOTALS": "VTOTALS", "vTotals": "vTotals_2"}


def test_a_name_too_long_for_unity_catalog_is_refused():
    with pytest.raises(ValueError, match="255"):
        extend(names.UNITY_CATALOG, ["x" * 256], {})
