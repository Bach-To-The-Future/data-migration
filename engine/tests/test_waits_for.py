"""A manual item that waits for a ticket says which, in a field, and its reason stays plain English.

Reasons reach client reports; a ticket number means nothing to a client. So a construct that is manual work only for
want of something not built yet (procedural generation, lowering assist, a support-library helper) carries the
ticket in `waits_for`, a tuple of references such as `("#87",)`, which the release gate and the ticket that builds
the helper search by. A construct that is manual work for good (an `xml` cast: the type is held as a string by
design) waits for nothing, and the gate does not count it (issues #75 and #110).

No database is involved: the representation is written out by `generation_ir`.
"""
import re

import pytest

import generation_ir as ir
from generation_ir import column, money_product, node
from migrt.targets.pyspark import generator, untranslatable

TICKET_IN_TEXT = re.compile(r"#\d+|\bissue\b", re.IGNORECASE)


def items(result: ir.Result):
    return result.file.manual_items


def one_item(result: ir.Result):
    [item] = items(result)
    return item


def in_select_list(expression, name: str = "Failing") -> ir.Result:
    expression.role = "output"
    expression.attrs["output_name"] = name
    return ir.generate(ir.view(node("project", "", ir.read_orders(), column("output", "Status"), expression)))


def no_ticket_numbers_in_the_text(result: ir.Result) -> None:
    for text in result.records:
        assert not TICKET_IN_TEXT.search(text), text


# ---------------------------------------------------------------- waiting for a support-library helper

def test_money_arithmetic_waits_for_the_number_helpers():
    result = in_select_list(money_product())

    assert one_item(result).waits_for == ("#87",)
    no_ticket_numbers_in_the_text(result)


def test_a_string_compared_by_a_named_rule_waits_for_the_comparison_helpers():
    compare = node("compare", "", column("argument", "Status"), column("argument", "Status"), type_=ir.BOOLEAN,
                   operator="=", rule={"collation": "Latin1_General_CI_AS"})

    result = in_select_list(compare)

    assert one_item(result).waits_for == ("#47",)
    no_ticket_numbers_in_the_text(result)


def test_bigint_division_waits_for_the_number_helpers():
    division = node("integer_divide", "", column("argument", "Quantity"), column("argument", "Quantity"),
                    type_=ir.VType.of("integer", bits=64, signed=True))

    assert one_item(in_select_list(division)).waits_for == ("#87",)


@pytest.mark.parametrize("target, expected", [
    (ir.STRING, ("#47",)),                                           # cut or padded to its length (Annex C 2.13)
    (ir.VType.of("decimal", precision=10, scale=2), ("#87",)),       # rounded to its scale (numbers)
])
def test_a_store_that_cuts_or_rounds_waits_for_the_helper_of_its_type(target, expected):
    store = node("implicit_convert", "", column("argument", "Status"), type_=target, fits=True)

    result = in_select_list(store)

    assert one_item(result).waits_for == expected
    no_ticket_numbers_in_the_text(result)


def test_a_cast_to_tinyint_waits_for_the_number_helpers():
    tinyint = node("implicit_convert", "", column("argument", "Status"),
                   type_=ir.VType.of("integer", bits=8, signed=False))

    result = in_select_list(tinyint)

    assert one_item(result).waits_for == ("#87",)
    no_ticket_numbers_in_the_text(result)


def test_an_xml_cast_is_manual_work_for_good_and_waits_for_nothing():
    xml = ir.VType.of("xml")
    parameter = node("declare_parameter", "parameter", type_=xml, name="@x", direction="input", readonly=False,
                     default=None)
    variable = node("variable", "output", type_=xml, name="@x", output_name="Doc")
    plan = node("project", "query", ir.read_orders(), variable)
    artefact = ir.Artefact(ir.VIEW, "view", "", node("view_body", "", parameter, plan), dependencies=[ir.TABLE])
    ir.position(artefact)

    result = ir.generate(artefact)

    assert "xml is a string" in one_item(result).reason
    assert one_item(result).waits_for == ()


# ---------------------------------------------------------------- waiting for a ticket that builds generation

def test_procedural_statements_wait_for_the_ticket_that_builds_their_kind():
    result = ir.generate(ir.routine("procedure", node("jump", "statement", label="done"), node("emit_message", "statement")))

    assert sorted(i.waits_for for i in items(result)) == [("#119",), ("#133",)]
    assert all("procedural generation" in i.reason for i in items(result))
    no_ticket_numbers_in_the_text(result)


def test_a_statement_lowering_could_not_express_waits_for_lowering_assist():
    opaque = node("opaque_fragment", "statement", reason="a construct lowering does not know")

    result = ir.generate(ir.routine("procedure", opaque))

    assert one_item(result).waits_for == ("#63",)
    assert "a construct lowering does not know" in one_item(result).reason
    no_ticket_numbers_in_the_text(result)


def test_a_scalar_function_that_is_more_than_one_expression_waits_for_raising_and_the_udf_fallback():
    result = ir.generate(ir.routine("scalar_function", node("return", "statement"), node("return", "statement"),
                                    identity=ir.FUNCTION))

    assert items(result) and all(i.waits_for == ("#64",) for i in items(result))
    no_ticket_numbers_in_the_text(result)


def test_reading_a_temp_table_waits_for_procedural_generation():
    temp = node("read_table", "input", table="#t", alias="t", temp_table=True)

    result = ir.generate(ir.view(node("project", "", temp, column("output", "Status", relation="t"))))

    assert one_item(result).waits_for == ("#120",)
    no_ticket_numbers_in_the_text(result)


# ---------------------------------------------------------------- what is not manual work for want of anything

def test_a_cross_join_is_the_generators_own_gap_and_waits_for_no_ticket():
    right = node("read_table", "right", table=ir.TABLE, alias="b")
    left = ir.read_orders()
    left.role = "left"
    join = node("join", "input", left, right)
    join.attrs["kind"] = "cross"
    plan = node("project", "", join, column("output", "Status"))

    assert one_item(ir.generate(ir.view(plan))).waits_for == ()


# ---------------------------------------------------------------- nothing waits for a ticket that is closed

@pytest.mark.parametrize("kind,ticket", [
    ("jump", "#133"), ("declare_label", "#133"),
    ("window", "#140"), ("row_number", "#140"), ("named_query", "#141"), ("wait", "#142"),
    ("role_membership", "#143"), ("catalog_lookup", "#143"), ("session_value", "#144"),
    ("for_each_row", "#64"), ("atomic_region", "#64"), ("named_result", "#64"), ("column_expression_function", "#64"),
    ("sort", "#128"), ("union", "#129"), ("read_values", "#129"), ("limit", "#129"), ("scalar_query", "#130"),
    ("exists", "#131"), ("convert", "#132"), ("try_convert", "#132"), ("convert_with_style", "#132"),
    ("concat_null_propagating", "#47"), ("trim", "#47"), ("upper", "#47"), ("left_part", "#47"),
    ("date_add", "#87"), ("current_time", "#87"), ("date_diff_boundaries", "#87"), ("round", "#87"),
    ("decimal_divide", "#87"), ("last_identity", "#87"), ("next_sequence_value", "#87"),
])
def test_a_kind_without_generation_waits_for_the_ticket_that_owns_it(kind, ticket):
    assert untranslatable.waits_for_kind(kind, generator.handled_kinds()) == (ticket,)


@pytest.mark.parametrize("kind", ["dynamic_sql_execution", "schema_change", "permission_change", "pivot", "lateral_join", "bulk_load"])
def test_a_kind_no_ticket_has_taken_waits_for_nothing_rather_than_for_a_ticket_that_is_closed(kind):
    assert untranslatable.waits_for_kind(kind, generator.handled_kinds()) == ()


def test_no_waits_for_in_the_engine_names_the_ticket_that_was_split_into_the_others():
    """#50 is closed: a manual item waits for the ticket that builds its kind, or for nothing."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "src"
    found = [str(p.relative_to(root)) for p in root.rglob("*.py") if '"#50"' in p.read_text(encoding="utf-8")]

    assert found == []


def test_a_non_number_type_in_procedural_code_waits_for_the_ticket_that_holds_the_types():
    from migrt.core.ir import VType
    assert generator.Generator.type_waits(VType.of("guid")) == ("#134",)
    assert generator.Generator.type_waits(VType.of("decimal", precision=9, scale=2)) == ("#87",)
