"""Layer 1, the PySpark target's code checks, at the target's contract: each check must
reject what it exists to catch."""
import pytest

from migrt.targets.pyspark import layer1

GOOD = '''"""A converted view."""
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


def checks(text, links=LINKS, spans=SPANS, manual=(), escape=()):
    result = layer1.run(text, links=links, node_spans=spans, manual_items=list(manual), escape_hatch_uses=list(escape))
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
