# PROTOTYPE: hand conversion of sales.uspGetOrdersByCustomer. Never run; migration_support does not exist.
from pyspark.sql import functions as F

from migration_support import ProcedureResult, Session, converted, tsql

from converted.RetailSample.sales.functions.fnOrderAgeDays import fnOrderAgeDays   # not converted in this prototype


@converted(artefact="sales.uspGetOrdersByCustomer", kind="procedure")
def uspGetOrdersByCustomer(session: Session, *, CustomerCode, FromDate=None, ToDate=None) -> ProcedureResult:
    # Parameters become typed literals: varchar(10) cuts a longer value without an error.
    p_CustomerCode = session.param(CustomerCode, "varchar(10)")
    p_FromDate = session.param(FromDate, "datetime")
    p_ToDate = session.param(ToDate, "datetime")

    o = session.table("sales.Order").alias("o")
    c = session.table("ref.Customer").alias("c")

    # src: SELECT ... FROM sales.[Order] AS o JOIN ref.Customer AS c ON c.CustomerId = o.CustomerId
    #      WHERE c.CustomerCode = @CustomerCode
    #        AND o.OrderDate >= ISNULL(@FromDate, '19000101') AND o.OrderDate < ISNULL(@ToDate, '99991231')
    #      ORDER BY o.OrderDate DESC;
    orders = (
        o.join(c, F.col("c.CustomerId") == F.col("o.CustomerId"))
        .where(tsql.ci_eq(F.col("c.CustomerCode"), p_CustomerCode))            # CustomerCode is case-insensitive
        .where(F.col("o.OrderDate") >= F.coalesce(p_FromDate, tsql.datetime("19000101")))
        .where(F.col("o.OrderDate") < F.coalesce(p_ToDate, tsql.datetime("99991231")))
        .select(
            F.col("o.OrderId"),
            F.col("o.OrderDate"),
            F.col("o.Status"),
            F.col("o.TotalAmount"),
            fnOrderAgeDays(session, F.col("o.OrderDate"), F.lit(None).cast("timestamp_ntz")).alias("AgeDays"),
        )
        .orderBy(F.col("o.OrderDate").desc())
    )
    return ProcedureResult(return_code=0, result_sets=[orders])
