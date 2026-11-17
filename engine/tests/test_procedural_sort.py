"""ORDER BY in generated code (issue #128, spec section 6): the rows of a result set come in the order SQL Server gives them, for
integer and date keys, ascending and descending, several keys, positions, aliases and expressions, with NULLs first
ascending and last descending. Rows with equal keys come in no defined order on either side, so each group of equal keys is
compared as a set. SQL Server is the oracle: every expected order below is what it returned.

A string key names a comparison rule, which the comparison helpers own: it is manual work that waits for them."""
import datetime as dt
import decimal
import re

import pytest

from generation_helpers import generate_text
from migrt.targets.pyspark import generator
from test_procedural_generation import Tables, procedure

SQL_TABLE = ("CREATE TABLE {s}.[Order] (OrderId int NOT NULL, CustomerId int NOT NULL, OrderDate datetime NOT NULL, "
             "ShippedAt datetime2(7) NULL, Status tinyint NOT NULL, Note nvarchar(200) NULL, TotalAmount money NOT NULL)")
D = dt.datetime
ROWS = [  # OrderId, CustomerId, OrderDate, ShippedAt, Status, Note, TotalAmount
    (1, 10, D(2026, 1, 5), None, 0, "a", decimal.Decimal("10.5")),
    (2, 11, D(2026, 1, 7), D(2026, 1, 9), 1, None, decimal.Decimal("3")),
    (3, 10, D(2026, 1, 7), D(2026, 1, 8), 1, "c", decimal.Decimal("30")),
    (4, 12, D(2026, 2, 1), None, 2, "b", decimal.Decimal("3")),
    (5, 11, D(2025, 12, 31), D(2026, 1, 2), 9, "a", decimal.Decimal("7.25")),
    (6, 12, D(2026, 1, 5), D(2026, 1, 9), 1, None, decimal.Decimal("30")),
    (7, 10, D(2026, 2, 1), None, 0, "d", decimal.Decimal("0")),
    (8, 13, D(2026, 1, 7), D(2026, 1, 8), 2, "b", decimal.Decimal("12")),
]
SPARK_SCHEMA = ("OrderId int, CustomerId int, OrderDate timestamp_ntz, ShippedAt timestamp_ntz, Status smallint, "
                "Note string, TotalAmount decimal(19, 4)")


def literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, dt.datetime):
        return f"'{value:%Y-%m-%d %H:%M:%S}'"
    if isinstance(value, dt.date):
        return f"'{value:%Y-%m-%d}'"
    return f"N'{value}'" if isinstance(value, str) else str(value)


def as_python(value):
    """A value as the SQL Server connection returns it: a date or time arrives as ISO text with seven fractional digits."""
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?", value):
        return dt.datetime.fromisoformat(value[:26])
    return value


def both(corpus_snapshot, duo, select_list: str, order_by: str, columns: list[str], group_by: str | None = None, *,
         table: str = "sales.[Order]", ddl: str = SQL_TABLE, rows: list = ROWS, schema: str = SPARK_SCHEMA):
    """The procedure `SELECT <select_list> FROM <table> AS o [GROUP BY ..] ORDER BY <order_by>` on SQL Server and generated, over the
    same rows (`ddl` makes the table on SQL Server): the rows of each, as tuples of `columns`, in the order each gave them."""
    grouping = f"\nGROUP BY {group_by}" if group_by else ""
    sql = f"CREATE PROCEDURE sales.uspSort AS BEGIN\nSELECT {select_list}\nFROM {table} AS o{grouping}\nORDER BY {order_by};\nEND"
    file, function = procedure(corpus_snapshot, sql, name="uspSort")
    assert file.file.manual_items == [], file.file.manual_items
    duo.tsql(ddl)
    name = table.split(".")[-1]
    for row in rows:
        duo.tsql(f"INSERT {{s}}.{name} VALUES ({', '.join(literal(v) for v in row)})")
    duo.tsql(sql.replace("sales.", "{s}.").replace("ref.", "{s}."))
    result, errors = duo.run("EXEC {s}.uspSort")
    assert not errors, errors
    expected = [tuple(as_python(row[c]) for c in columns) for row in result]
    [read] = re.findall(r'spark\.read\.table\("([^"]+)"\)', file.text)[:1]
    ss = duo.session()
    ss.spark = Tables(ss.spark, {read: duo.spark.createDataFrame(rows, schema)})
    [frame] = function(ss).result_sets
    actual = [tuple(r[c] for c in columns) for r in frame.collect()]
    return expected, actual, file


def groups(rows: list[tuple], key_columns: int):
    """The rows cut into runs of equal keys, each run a set: what is defined of an order with ties."""
    out: list[tuple] = []
    for row in rows:
        if out and out[-1][0] == row[:key_columns]:
            out[-1][1].append(row)
        else:
            out.append((row[:key_columns], [row]))
    return [(key, sorted(run, key=repr)) for key, run in out]


CASES = [   # what is selected, what it is ordered by, the columns compared
    ("an integer key ascending", "o.OrderId, o.CustomerId", "o.CustomerId, o.OrderId", ["OrderId", "CustomerId"]),
    ("an integer key descending", "o.OrderId, o.CustomerId", "o.CustomerId DESC, o.OrderId DESC", ["OrderId", "CustomerId"]),
    ("a date key descending", "o.OrderId, o.OrderDate", "o.OrderDate DESC, o.OrderId", ["OrderId", "OrderDate"]),
    ("a nullable date key: NULLs first ascending", "o.OrderId, o.ShippedAt", "o.ShippedAt, o.OrderId",
     ["OrderId", "ShippedAt"]),
    ("a nullable date key: NULLs last descending", "o.OrderId, o.ShippedAt", "o.ShippedAt DESC, o.OrderId",
     ["OrderId", "ShippedAt"]),
    ("several keys, mixed directions", "o.OrderId, o.Status, o.CustomerId", "o.Status DESC, o.CustomerId, o.OrderId DESC",
     ["OrderId", "Status", "CustomerId"]),
    ("a position", "o.OrderId, o.Status", "2 DESC, 1", ["OrderId", "Status"]),
    ("an alias", "o.OrderId, o.Status AS Kind", "Kind, OrderId DESC", ["OrderId", "Kind"]),
    ("an expression", "o.OrderId, o.CustomerId", "o.CustomerId * 2 DESC, o.OrderId", ["OrderId", "CustomerId"]),
    ("a column that is not in the select list", "o.OrderId", "o.Status DESC, o.OrderId", ["OrderId"]),
]


@pytest.mark.parametrize("name,select_list,order_by,columns", CASES, ids=[c[0] for c in CASES])
def test_the_rows_come_in_sql_servers_order(corpus_snapshot, duo, name, select_list, order_by, columns):
    expected, actual, file = both(corpus_snapshot, duo, select_list, order_by, columns)

    assert actual == expected, name
    assert len(expected) == len(ROWS)


def test_grouped_rows_sorted_by_a_column_of_the_select_list_come_in_sql_servers_order(corpus_snapshot, duo):
    expected, actual, _ = both(corpus_snapshot, duo, "o.CustomerId, COUNT(*) AS N", "N DESC, CustomerId",
                               ["CustomerId", "N"], group_by="o.CustomerId")

    assert actual == expected and len(expected) == 4


def test_grouped_rows_sorted_by_an_expression_over_them_are_manual_work(corpus_snapshot):
    sql = ("CREATE PROCEDURE sales.uspSort AS BEGIN\nSELECT o.CustomerId, COUNT(*) AS N FROM sales.[Order] AS o "
           "GROUP BY o.CustomerId ORDER BY COUNT(*) DESC;\nEND")
    file = generate_text(corpus_snapshot, sql, kind="procedure", name="uspSort")

    assert file.layer1.passed, file.layer1.failures
    assert file.file.manual_items and not any("#" in m.reason for m in file.file.manual_items)
    assert any("grouped rows" in m.reason for m in file.file.manual_items)


def test_a_bit_key_and_a_date_key_with_nulls_come_in_sql_servers_order(corpus_snapshot, duo):
    ddl = "CREATE TABLE {s}.Customer (CustomerId int NOT NULL, IsActive bit NOT NULL, DeletedAt datetime NULL)"
    rows = [(1, True, None), (2, False, D(2026, 3, 1)), (3, True, D(2026, 1, 1)), (4, False, None), (5, True, D(2026, 2, 1))]
    expected, actual, _ = both(corpus_snapshot, duo, "o.CustomerId, o.IsActive, o.DeletedAt",
                               "o.IsActive DESC, o.DeletedAt, o.CustomerId", ["CustomerId", "DeletedAt"],
                               table="ref.Customer", ddl=ddl, rows=rows,
                               schema="CustomerId int, IsActive boolean, DeletedAt timestamp_ntz")

    assert [row[0] for row in actual] == [row[0] for row in expected] == [1, 3, 5, 4, 2]


def test_a_date_key_that_is_a_date_comes_in_sql_servers_order(corpus_snapshot, duo):
    ddl = "CREATE TABLE {s}.Product (ProductId int NOT NULL, DiscontinuedOn date NULL)"
    rows = [(1, dt.date(2026, 5, 1)), (2, None), (3, dt.date(2025, 1, 1)), (4, dt.date(2026, 5, 1)), (5, None)]
    expected, actual, _ = both(corpus_snapshot, duo, "o.ProductId, o.DiscontinuedOn", "o.DiscontinuedOn DESC, o.ProductId",
                               ["ProductId"], table="ref.Product", ddl=ddl, rows=rows,
                               schema="ProductId int, DiscontinuedOn date")

    assert actual == expected == [(1,), (4,), (3,), (2,), (5,)]


def test_the_rows_of_a_function_that_is_sorted_are_ordered_only_for_a_read_of_the_function_itself():
    """A view or an inline function is a query another query reads, and a query that reads it may list its rows in another order: the
    report says so where one is sorted."""
    import generation_ir as ir
    from generation_ir import column, node
    order = column("order", "Status")
    order.attrs["direction"] = "ascending"
    plan = node("sort", "", node("project", "input", ir.read_orders(), column("output", "OrderId")), order)

    result = ir.generate(ir.view(plan))

    assert result.layer1.passed, result.layer1.failures
    assert any("read of it" in a or "reads it" in a for a in result.file.assumptions) and not result.file.manual_items


def test_rows_with_equal_keys_are_compared_as_a_set_since_neither_side_orders_them(corpus_snapshot, duo):
    expected, actual, file = both(corpus_snapshot, duo, "o.Status, o.OrderId", "o.Status", ["Status", "OrderId"])

    assert groups(actual, 1) == groups(expected, 1)
    assert [k for k, _ in groups(actual, 1)] == [(0,), (1,), (2,), (9,)]


def test_a_sort_with_no_defined_order_among_ties_says_so_in_the_report(corpus_snapshot):
    sql = "CREATE PROCEDURE sales.uspSort AS BEGIN\nSELECT o.OrderId, o.Status FROM sales.[Order] AS o ORDER BY o.Status;\nEND"
    file = generate_text(corpus_snapshot, sql, kind="procedure", name="uspSort")

    assert file.layer1.passed, file.layer1.failures
    assert any("equal sort keys" in a and "#" not in a for a in file.file.assumptions)


def test_a_string_key_is_manual_work_that_waits_for_the_comparison_helpers(corpus_snapshot):
    sql = "CREATE PROCEDURE sales.uspSort AS BEGIN\nSELECT c.CustomerCode FROM ref.Customer AS c ORDER BY c.CustomerCode;\nEND"
    file = generate_text(corpus_snapshot, sql, kind="procedure", name="uspSort")

    assert file.layer1.passed, file.layer1.failures
    [item] = file.file.manual_items
    assert item.waits_for == ("#47",) and "#" not in item.reason and "sort" in item.reason


def test_sort_is_handled_by_the_generator():
    assert "sort" in generator.handled_kinds()
