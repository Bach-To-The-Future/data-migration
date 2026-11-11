"""What generation leaves behind when a construct it cannot generate is given up (CODING_STANDARDS: nothing
degrades silently; spec section 5, "Generation").

A construct generation cannot produce exactly becomes a manual item, and the code it had begun to write for it is
not in the file. So nothing that code did may remain: no import for a function it called, no setting recorded as
read for a secret it would have read, no caveat for an operation it would have performed. A failure is given up in
three places in `generator.py`: a plan step as a whole (the function raises when called), one output column of a
select list, and one output of an aggregate. The first undoes what it began (`restore`); the other two do not.

Each case builds the failing construct so that its sub-expression is generated first (and so leaves its mark) and
a later sub-expression is the one the target cannot generate (money arithmetic). The "filter" site is the control:
its failure is given up with the whole function, which restores, so it holds today and must keep holding.

No database is involved: the representation is written out by `generation_ir`.
"""
import pytest

import generation_ir as ir
from generation_ir import column, count_rows, money_product, node

SECRET = f"{ir.VIEW}.secret_1"
SITES = ["select list", "aggregate", "filter"]


def failing_call():
    """A call of a generated scalar function: its import is added before its arguments are generated."""
    return node("call_scalar_function", "argument", count_rows(), money_product(), type_=ir.INT, function=ir.FUNCTION)


def failing_secret():
    """A comparison with a literal inside a marked secret, generated (and its setting recorded) before the rest."""
    secret = node("literal", "argument", type_=ir.STRING, value="x", secret_setting=SECRET)
    return node("and", "argument", node("compare", "argument", secret, count_rows(), type_=ir.BOOLEAN, operator="="),
                money_product(), type_=ir.BOOLEAN)


def failing_sum():
    """An integer sum, which carries a caveat once generated (silent difference 3.14)."""
    return node("add", "argument", node("sum", "argument", column("argument", "Quantity"), type_=ir.INT),
                money_product(), type_=ir.INT)


def place(expression, site: str) -> ir.Result:
    """A view in which `expression` fails in the given place, generated."""
    if site == "filter":
        plan = node("project", "", node("filter", "input", ir.read_orders(), _as("condition", expression)),
                    column("output", "Status"))
    elif site == "aggregate":
        plan = node("aggregate", "", ir.read_orders(), column("key", "Status"), column("output", "Status"),
                    _as("output", expression, "Failing"))
    else:
        plan = node("project", "", ir.read_orders(), column("output", "Status"), _as("output", expression, "Failing"))
    result = ir.generate(ir.view(plan))
    # The case must really fail where it says, or a test of what is left behind would pass for nothing.
    assert any("money" in m.reason.lower() for m in result.file.manual_items), result.records
    return result


def _as(role: str, expression, output_name: str | None = None):
    expression.role = role
    if output_name:
        expression.attrs["output_name"] = output_name
    return expression


@pytest.mark.parametrize("site", SITES)
def test_a_given_up_expression_leaves_no_import_for_a_function_it_called(site):
    result = place(failing_call(), site)

    imports = [line for line in result.text.splitlines() if line.startswith("from converted")]
    assert imports == [], "the file imports a function that no code in it calls"


@pytest.mark.parametrize("site", SITES)
def test_a_given_up_expression_does_not_have_a_secret_recorded_as_read(site):
    result = place(failing_secret(), site)

    assert "ss.setting" not in result.text
    assert result.file.settings_used == [], "the manifest says the entry point reads a setting no code reads"


@pytest.mark.parametrize("site", SITES)
def test_a_given_up_expression_leaves_no_caveat_on_code_that_is_not_in_the_file(site):
    result = place(failing_sum(), site)

    assert set(result.file.differences) <= set(result.file.expression_nodes), \
        "a silent-difference caveat is reported on a node whose code the file does not hold"


@pytest.mark.parametrize("site", SITES)
def test_a_file_with_a_given_up_expression_still_passes_layer_1(site):
    result = place(failing_call(), site)

    assert result.layer1.passed, result.layer1.failures


def test_a_failing_column_generated_without_its_cast_says_which_column_and_why():
    """A manual column is cast to its type where the type has one; where it has none the column is emitted bare."""
    plan = node("project", "", ir.read_orders(), column("output", "Status"),
                column("output", "Shape", ir.NO_TYPE))
    result = ir.generate(ir.view(plan))

    assert any("no neutral meaning" in m.reason for m in result.file.manual_items), result.records
    assert any("Shape" in record and "Spark type" in record for record in result.records), \
        "nothing in the report says the column Shape has no Spark type"
