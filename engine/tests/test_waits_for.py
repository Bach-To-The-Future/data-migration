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

def test_procedural_statements_wait_for_procedural_generation():
    result = ir.generate(ir.routine("procedure", node("if", "statement"), node("set_session_option", "statement")))

    assert items(result) and all(i.waits_for == ("#50",) for i in items(result))
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

    assert one_item(result).waits_for == ("#50",)
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
