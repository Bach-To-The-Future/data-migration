# PROTOTYPE: hand conversion of sales.uspRecalculateDailySales, in two forms. Never run; migration_support does not exist.
from pyspark.sql import functions as F

from migration_support import ProcedureResult, Session, converted, tsql


def _line_revenue():
    # src: ol.Quantity * ol.UnitPrice * (100 - ol.DiscountPct) / 100, in money
    return tsql.money(
        tsql.money(tsql.money(F.col("ol.Quantity") * F.col("ol.UnitPrice")) * (F.lit(100) - F.col("ol.DiscountPct")))
        / F.lit(100)
    )


# --------------------------------------------------------------------------- faithful form

@converted(artefact="sales.uspRecalculateDailySales", kind="procedure", form="faithful")
def uspRecalculateDailySales(session: Session, *, FromDate, ToDate) -> ProcedureResult:
    p_FromDate = session.param(FromDate, "date")
    p_ToDate = session.param(ToDate, "date")

    # src: DECLARE @Day date = @FromDate; DECLARE @Deleted int = 0; DECLARE @Loaded int = 0;
    Day = session.value(FromDate, "date")
    Deleted = 0
    Loaded = 0

    # src: CREATE TABLE #Sales (... PRIMARY KEY (SalesDate, ProductId));
    session.create_temp_table(
        "#Sales",
        columns={"SalesDate": "date not null", "ProductId": "int not null", "Units": "int not null", "Revenue": "money not null"},
        primary_key=("SalesDate", "ProductId"),
    )

    o = session.table("sales.Order").alias("o")
    ol = session.table("sales.OrderLine").alias("ol")

    # src: WHILE @Day <= @ToDate
    # Either side may be NULL, and NULL <= x is unknown, which ends the loop. Python's <= would raise instead,
    # so the comparison goes through the scalar helper.
    while tsql.scalar.le(Day, session.value(ToDate, "date")):
        # src: INSERT INTO #Sales (...) SELECT @Day, ol.ProductId, SUM(ol.Quantity), SUM(<money>)
        #      FROM sales.[Order] AS o JOIN sales.OrderLine AS ol ON ol.OrderId = o.OrderId
        #      WHERE CAST(o.OrderDate AS date) = @Day AND o.Status <> 9 GROUP BY ol.ProductId;
        session.insert(
            "#Sales",
            o.join(ol, F.col("ol.OrderId") == F.col("o.OrderId"))
            .where(F.col("o.OrderDate").cast("date") == F.lit(Day))
            .where(F.col("o.Status") != 9)
            .groupBy(F.col("ol.ProductId"))
            .agg(F.sum("ol.Quantity").cast("int").alias("Units"), tsql.money(F.sum(_line_revenue())).alias("Revenue"))
            .select(F.lit(Day).alias("SalesDate"), "ProductId", "Units", "Revenue"),
        )
        # src: SET @Day = DATEADD(day, 1, @Day);
        Day = tsql.scalar.dateadd("day", 1, Day)

    # src: BEGIN TRANSACTION; DELETE ...; SET @Deleted = @@ROWCOUNT; INSERT ...; SET @Loaded = @@ROWCOUNT; COMMIT;
    # CAVEAT for the report (ADR 0004): if the INSERT failed, the original would go on to COMMIT the DELETE.
    # This raises, and the block rolls the DELETE back.
    with session.transaction():
        session.delete("sales.DailySales", where=F.col("SalesDate").between(p_FromDate, p_ToDate))
        Deleted = session.rowcount

        session.insert("sales.DailySales", session.table("#Sales").select("SalesDate", "ProductId", "Units", "Revenue"))
        Loaded = session.rowcount

    # src: DROP TABLE #Sales;
    session.drop_temp_table("#Sales")

    # src: SELECT @Deleted AS RowsDeleted, @Loaded AS RowsLoaded;
    return ProcedureResult(
        return_code=0,
        result_sets=[session.row(RowsDeleted=(Deleted, "int"), RowsLoaded=(Loaded, "int"))],
    )


# --------------------------------------------------------------------------- raised form

@converted(artefact="sales.uspRecalculateDailySales", kind="procedure", form="raised",
           transforms=("counted_loop_to_group_by", "temp_table_to_dataframe"))
def uspRecalculateDailySales_raised(session: Session, *, FromDate, ToDate) -> ProcedureResult:
    """The day-by-day loop raised to one grouped query, and #Sales raised to a DataFrame.

    What had to be true:
    - the loop variable only steps one day at a time from @FromDate to @ToDate, and the body uses it
      only as a filter value and an output value, so the loop is a GROUP BY over that range;
    - #Sales is filled once and then only read, and the tables it is built from (sales.Order, sales.OrderLine)
      are not written before its last use.
    """
    p_FromDate = session.param(FromDate, "date")
    p_ToDate = session.param(ToDate, "date")

    o = session.table("sales.Order").alias("o")
    ol = session.table("sales.OrderLine").alias("ol")
    order_day = F.col("o.OrderDate").cast("date")

    sales = (
        o.join(ol, F.col("ol.OrderId") == F.col("o.OrderId"))
        .where(order_day.between(p_FromDate, p_ToDate))   # NULL bounds select nothing, as the loop would run zero times
        .where(F.col("o.Status") != 9)
        .groupBy(order_day.alias("SalesDate"), F.col("ol.ProductId"))
        .agg(F.sum("ol.Quantity").cast("int").alias("Units"), tsql.money(F.sum(_line_revenue())).alias("Revenue"))
    )

    with session.transaction():
        session.delete("sales.DailySales", where=F.col("SalesDate").between(p_FromDate, p_ToDate))
        Deleted = session.rowcount

        session.insert("sales.DailySales", sales.select("SalesDate", "ProductId", "Units", "Revenue"))
        Loaded = session.rowcount

    return ProcedureResult(
        return_code=0,
        result_sets=[session.row(RowsDeleted=(Deleted, "int"), RowsLoaded=(Loaded, "int"))],
    )
