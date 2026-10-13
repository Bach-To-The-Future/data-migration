# PROTOTYPE: hand conversion of the batch customer_dedup.sql. Never run; migration_support does not exist.
from pyspark.sql import Window, functions as F

from migration_support import BatchResult, Session, converted, tsql


@converted(artefact="customer_dedup", kind="batch")
def batch_1(session: Session) -> BatchResult:
    staged = session.table("stg.CustomerImport")

    # src: WITH Ranked AS (SELECT CustomerCode, ROW_NUMBER() OVER (PARTITION BY CustomerCode
    #                      ORDER BY LoadedAt DESC, LEN(Name) DESC) AS rn
    #                      FROM stg.CustomerImport WHERE CustomerCode IS NOT NULL)
    #      DELETE FROM Ranked WHERE rn > 1;
    #
    # CustomerCode is case-insensitive, so the partition is on its normalised value.
    # The DELETE's condition is a window function, which no row-by-row delete condition can express,
    # and the table has no key to name the rows by. So the statement is turned inside out:
    # keep every row the original would have kept, and replace the table's contents with them.
    order = Window.partitionBy(tsql.ci_key(F.col("CustomerCode"))).orderBy(
        F.col("LoadedAt").desc(), tsql.len(F.col("Name")).desc()
    )
    ranked = staged.where(F.col("CustomerCode").isNotNull()).withColumn("rn", F.row_number().over(order))
    kept = ranked.where(F.col("rn") == 1).drop("rn").unionByName(staged.where(F.col("CustomerCode").isNull()))

    # CAVEAT for the report: when two rows tie on LoadedAt and LEN(Name), SQL Server does not
    # define which survives, and neither does this.
    session.replace_rows("stg.CustomerImport", kept, as_statement="DELETE")   # sets @@ROWCOUNT to rows removed

    # src: SELECT COUNT(DISTINCT CustomerCode) AS DistinctCodes,
    #             SUM(CASE WHEN CustomerCode IS NULL THEN 1 ELSE 0 END) AS RowsWithoutCode,
    #             COUNT(*) AS RowsRemaining
    #      FROM stg.CustomerImport;
    summary = session.table("stg.CustomerImport").agg(
        F.countDistinct(tsql.ci_key(F.col("CustomerCode"))).cast("int").alias("DistinctCodes"),
        F.sum(F.when(F.col("CustomerCode").isNull(), F.lit(1)).otherwise(F.lit(0))).cast("int").alias("RowsWithoutCode"),
        F.count(F.lit(1)).cast("int").alias("RowsRemaining"),
    )
    return BatchResult(result_sets=[summary])


BATCHES = [batch_1]
