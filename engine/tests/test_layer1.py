"""Layer 1, the PySpark target's code checks, at the target's contract: each check must
reject what it exists to catch."""
import pytest

from migrt.targets.pyspark import layer1

GOOD = '''"""A generated view."""
from pyspark.sql import DataFrame
from pyspark.sql import functions as F


def v(ss) -> DataFrame:
    """Doc."""
    spark = ss.spark
    spark.conf.set("spark.sql.ansi.enabled", "true")
    t = spark.read.table("`db`.`s`.`t`").alias("t")
    return t.select(F.col("t.a").alias("a"))
'''
LINKS = {10: ["n2"], 11: ["n1", "n3"]}
SPANS = {"n1": "L1:1-L1:20", "n2": "L1:15-L1:20", "n3": "L1:8-L1:9"}


def span_of(text, snippet, line=None):
    """Where `snippet` sits in the file: the line (1-based) and the columns, as generation records them."""
    lines = text.splitlines()
    at = next(i for i, code in enumerate(lines, start=1) if snippet in code and line in (None, i))
    start = lines[at - 1].index(snippet)
    return layer1.Span(at, start, start + len(snippet))


EXPRESSIONS = {"n3": span_of(GOOD, 'F.col("t.a")')}


def checks(text, links=LINKS, spans=SPANS, manual=(), escape=(), expressions=None, expected=("n3",)):
    result = layer1.run(text, links=links, node_spans=spans, manual_items=list(manual), escape_hatch_uses=list(escape),
                        expressions=EXPRESSIONS if expressions is None else expressions,
                        expression_nodes=list(expected))
    return {c.name: c for c in result.checks}


def test_well_formed_code_passes_every_check():
    assert all(c.passed for c in checks(GOOD).values())


@pytest.mark.parametrize("name, text", [
    ("parses", GOOD.replace("return t", "return (t")),
    ("lint", GOOD.replace('return t.select(F.col("t.a")', 'return t.select(G.col("t.a")')),
    ("lint", GOOD.replace("    return t.select", "    unused = 1\n    return t.select")),
    ("import allow-list", GOOD.replace("from pyspark.sql import DataFrame", "import os\nfrom pyspark.sql import DataFrame")
     .replace('alias("a"))', 'alias(os.sep))')),
    ("ANSI mode pinned", GOOD.replace('    spark.conf.set("spark.sql.ansi.enabled", "true")\n', "")),
    ("ANSI mode pinned", GOOD.replace('    return t', '    spark.conf.set("spark.sql.ansi.enabled", "false")\n    return t')),
    ("writes through session state", GOOD.replace("    return t", "    t.write.saveAsTable('x')\n    return t")),
    ("escape hatch accounted for", GOOD.replace('t.select(F.col("t.a")', 'spark.sql("select a from t").select(F.col("a")')),
])
def test_each_check_rejects_what_it_exists_to_catch(name, text):
    found = checks(text)
    assert not found[name].passed, found[name]


def test_a_code_line_linked_to_no_node_fails_source_positions():
    assert not checks(GOOD, links={10: ["n2"]})["source positions"].passed


def test_a_node_without_a_source_position_fails_source_positions():
    assert not checks(GOOD, spans={**SPANS, "n3": None})["source positions"].passed


def test_a_manual_item_must_be_both_marked_in_the_code_and_declared():
    marked = GOOD.replace('F.col("t.a")', 'F.raise_error(F.lit("Manual work 1: no generation"))')
    assert checks(marked, manual=["M1"])["manual items accounted for"].passed
    assert not checks(marked)["manual items accounted for"].passed
    assert not checks(GOOD, manual=["M1"])["manual items accounted for"].passed


# ---------------------------------------------------------------- linked expressions are self-contained spans

def test_a_linked_expression_that_stands_alone_passes():
    assert checks(GOOD)["linked expressions are self-contained spans"].passed


def test_a_linked_expression_with_no_span_fails():
    found = checks(GOOD, expressions={})["linked expressions are self-contained spans"]
    assert not found.passed and "n3" in found.detail


def test_a_span_that_is_not_one_complete_expression_fails():
    broken = {"n3": layer1.Span(EXPRESSIONS["n3"].line, EXPRESSIONS["n3"].start, EXPRESSIONS["n3"].end - 1)}
    assert not checks(GOOD, expressions=broken)["linked expressions are self-contained spans"].passed


def test_a_span_that_reads_a_local_variable_is_not_self_contained():
    """`t` is a DataFrame built on an earlier line: evaluating the span alone could not find it."""
    reads_local = GOOD.replace('F.col("t.a")', 't["a"]')
    found = checks(reads_local, expressions={"n3": span_of(reads_local, 't["a"]')})
    assert not found["linked expressions are self-contained spans"].passed
    assert "t" in found["linked expressions are self-contained spans"].detail


def test_a_span_may_read_the_functions_parameters_and_the_imported_names():
    with_parameter = GOOD.replace("def v(ss)", "def v(ss, amount)").replace('F.col("t.a")', "F.lit(amount)")
    found = checks(with_parameter, expressions={"n3": span_of(with_parameter, "F.lit(amount)")})
    assert found["linked expressions are self-contained spans"].passed


def test_two_spans_that_overlap_without_one_holding_the_other_fail():
    inner = span_of(GOOD, '"t.a"')
    crossing = {"n3": layer1.Span(inner.line, inner.start - 6, inner.end), "n1": layer1.Span(inner.line, inner.start, inner.end + 1)}
    assert not checks(GOOD, expressions=crossing, expected=("n3", "n1"))["linked expressions are self-contained spans"].passed


def test_a_span_nested_in_another_is_fine():
    nested = {"n3": EXPRESSIONS["n3"], "n1": span_of(GOOD, '"t.a"')}
    assert checks(GOOD, expressions=nested, expected=("n3", "n1"))["linked expressions are self-contained spans"].passed


def test_a_span_outside_every_function_fails():
    assert not checks(GOOD, expressions={"n3": layer1.Span(1, 0, 10)})["linked expressions are self-contained spans"].passed
