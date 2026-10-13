# PROTOTYPE: hand conversion of sales.vOrderTotals. Never run; migration_support does not exist.
from pyspark.sql import DataFrame, functions as F

from migration_support import Session, converted, tsql


@converted(artefact="sales.vOrderTotals", kind="view")
def vOrderTotals(session: Session) -> DataFrame:
    o = session.table("sales.Order").alias("o")
    ol = session.table("sales.OrderLine").alias("ol")

    # Quantity int * UnitPrice money -> money; (100 - DiscountPct) int; money / int -> money.
    # money keeps four places and truncates, so each step is cast back.
    line_total = tsql.money(
        tsql.money(tsql.money(F.col("ol.Quantity") * F.col("ol.UnitPrice")) * (F.lit(100) - F.col("ol.DiscountPct")))
        / F.lit(100)
    )

    # src: FROM sales.[Order] AS o LEFT JOIN sales.OrderLine AS ol ON ol.OrderId = o.OrderId
    joined = o.join(ol, F.col("ol.OrderId") == F.col("o.OrderId"), "left")

    # src: GROUP BY o.OrderId, o.CustomerId, o.OrderDate, o.Status, o.TotalAmount
    # No grouping column is a string, so no collation handling is needed.
    grouped = joined.groupBy("o.OrderId", "o.CustomerId", "o.OrderDate", "o.Status", "o.TotalAmount")

    line_count = F.count("ol.OrderLineId").cast("int")   # COUNT returns int in T-SQL, bigint in Spark
    units = F.sum("ol.Quantity").cast("int")             # SUM(int) is int in T-SQL; the cast raises on overflow, as T-SQL does

    return grouped.agg(
        line_count.alias("LineCount"),
        F.coalesce(units, F.lit(0)).alias("Units"),                                   # ISNULL(SUM(...), 0)
        tsql.avg_int(F.col("ol.Quantity")).alias("AvgUnitsPerLine"),                  # AVG over int truncates; it counts non-null Quantity, not lines
        tsql.int_div(units, F.nullif(line_count, F.lit(0))).alias("UnitsPerLine"),    # int / NULLIF(int, 0)
        F.coalesce(tsql.money(F.sum(line_total)), tsql.money(F.lit(0))).alias("ComputedTotal"),
    ).select(
        "OrderId", "CustomerId", "OrderDate", "Status",
        "LineCount", "Units", "AvgUnitsPerLine", "UnitsPerLine", "ComputedTotal", "TotalAmount",
    )
