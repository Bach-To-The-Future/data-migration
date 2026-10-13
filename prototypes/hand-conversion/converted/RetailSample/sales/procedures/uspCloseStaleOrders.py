# PROTOTYPE: hand conversion of sales.uspCloseStaleOrders, in two forms. Never run; migration_support does not exist.
from pyspark.sql import functions as F

from migration_support import ProcedureResult, Session, converted, tsql

CANCEL_NOTE = "[auto-cancelled: empty]"


def _stale_orders(session: Session, p_AsOf, p_StaleAfter):
    # src: SELECT o.OrderId, o.OrderDate, o.Status FROM sales.[Order] AS o
    #      WHERE o.Status IN (0, 1) AND DATEDIFF(day, o.OrderDate, @AsOf) > @StaleAfter ORDER BY o.OrderDate;
    o = session.table("sales.Order").alias("o")
    return (
        o.where(F.col("o.Status").isin(0, 1))
        .where(tsql.datediff("day", F.col("o.OrderDate"), p_AsOf) > p_StaleAfter)
        .select(F.col("o.OrderId"), F.col("o.OrderDate"), F.col("o.Status"))
        .orderBy(F.col("o.OrderDate"))
    )


def _cancelled_note():
    # src: Note = ISNULL(Note + N' ', N'') + N'[auto-cancelled: empty]'
    return F.concat(F.coalesce(F.concat(F.col("Note"), F.lit(" ")), F.lit("")), F.lit(CANCEL_NOTE))


# --------------------------------------------------------------------------- faithful form

@converted(artefact="sales.uspCloseStaleOrders", kind="procedure", form="faithful")
def uspCloseStaleOrders(session: Session, *, AsOf, StaleAfter=30, DryRun=0) -> ProcedureResult:
    """The cursor kept as a row-by-row loop on the driver. Flagged in the report as row-by-row."""
    p_AsOf = session.param(AsOf, "datetime")
    p_StaleAfter = session.param(StaleAfter, "int")

    Closed = 0
    Cancelled = 0

    # src: DECLARE stale_orders CURSOR LOCAL FAST_FORWARD FOR ...; OPEN; FETCH NEXT ...; WHILE @@FETCH_STATUS = 0
    # The rows are a snapshot taken when the cursor opens, streamed one at a time.
    # CAVEAT for the report: the loop updates sales.Order, the table the cursor reads. SQL Server does not
    # promise whether a FAST_FORWARD cursor sees such changes. Here it never does.
    for OrderId, OrderDate, Status in session.cursor(_stale_orders(session, p_AsOf, p_StaleAfter)):

        # src: SELECT @Lines = COUNT(*) FROM sales.OrderLine WHERE OrderId = @OrderId;
        Lines = session.scalar(
            session.table("sales.OrderLine").where(F.col("OrderId") == F.lit(OrderId)).agg(F.count(F.lit(1)).cast("int"))
        )

        if Lines == 0:
            # src: IF @DryRun = 0 UPDATE sales.[Order] SET Status = 9, Note = ... WHERE OrderId = @OrderId;
            if DryRun == 0:   # a NULL @DryRun compares as unknown in T-SQL and as False here: same branch
                session.update(
                    "sales.Order",
                    set={"Status": F.lit(9), "Note": _cancelled_note()},
                    where=F.col("OrderId") == F.lit(OrderId),
                )
            Cancelled += 1
        elif Status == 1:
            # src: IF @DryRun = 0 UPDATE sales.[Order] SET Status = 2 WHERE OrderId = @OrderId;
            if DryRun == 0:
                session.update("sales.Order", set={"Status": F.lit(2)}, where=F.col("OrderId") == F.lit(OrderId))
            Closed += 1
        else:
            # src: PRINT 'Order ' + CAST(@OrderId AS varchar(12)) + ' is stale but not shipped; left open.';
            session.print(f"Order {OrderId} is stale but not shipped; left open.")

    # src: SELECT @Closed AS OrdersClosed, @Cancelled AS OrdersCancelled;
    return ProcedureResult(
        return_code=0,
        result_sets=[session.row(OrdersClosed=(Closed, "int"), OrdersCancelled=(Cancelled, "int"))],
    )


# --------------------------------------------------------------------------- raised form

@converted(artefact="sales.uspCloseStaleOrders", kind="procedure", form="raised",
           transforms=("cursor_to_set_based",))
def uspCloseStaleOrders_raised(session: Session, *, AsOf, StaleAfter=30, DryRun=0) -> ProcedureResult:
    """The cursor loop raised to set-based steps.

    What had to be true for this to be the same program:
    - each pass reads only its own cursor row and sales.OrderLine, which no pass writes;
    - each pass writes only its own order, so no pass changes what a later pass sees;
    - the counters only ever add one, so they are row counts;
    - the PRINT order is the cursor's order, so the leftover rows are listed in that order.
    """
    p_AsOf = session.param(AsOf, "datetime")
    p_StaleAfter = session.param(StaleAfter, "int")

    # The per-row "SELECT @Lines = COUNT(*)" becomes one count per order, joined in.
    line_counts = session.table("sales.OrderLine").groupBy("OrderId").agg(F.count(F.lit(1)).cast("int").alias("Lines"))
    stale = (
        _stale_orders(session, p_AsOf, p_StaleAfter).alias("s")
        .join(line_counts.alias("lc"), F.col("lc.OrderId") == F.col("s.OrderId"), "left")
        .select("s.OrderId", "s.OrderDate", "s.Status", F.coalesce(F.col("lc.Lines"), F.lit(0)).alias("Lines"))
    )
    # The three branches of the IF become three disjoint sets. They are fixed here, before any update,
    # because the updates below change the Status this query reads.
    stale = session.snapshot(stale)
    to_cancel = stale.where(F.col("Lines") == 0)
    to_close = stale.where((F.col("Lines") != 0) & (F.col("Status") == 1))
    left_open = stale.where((F.col("Lines") != 0) & (F.col("Status") != 1))

    Cancelled = to_cancel.count()
    Closed = to_close.count()

    if DryRun == 0:
        # A set of keys is passed as a DataFrame, never collected to the driver.
        session.update_from("sales.Order", source=to_cancel.select("OrderId"), on=("OrderId",),
                            set={"Status": F.lit(9), "Note": _cancelled_note()})
        session.update_from("sales.Order", source=to_close.select("OrderId"), on=("OrderId",),
                            set={"Status": F.lit(2)})

    for (OrderId,) in session.cursor(left_open.orderBy("OrderDate").select("OrderId")):
        session.print(f"Order {OrderId} is stale but not shipped; left open.")

    return ProcedureResult(
        return_code=0,
        result_sets=[session.row(OrdersClosed=(Closed, "int"), OrdersCancelled=(Cancelled, "int"))],
    )
