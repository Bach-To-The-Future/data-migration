# PROTOTYPE: hand conversion of sales.trOrder_AuditStatus. Never run; migration_support does not exist.
from pyspark.sql import DataFrame, functions as F

from migration_support import Session, converted


@converted(artefact="sales.trOrder_AuditStatus", kind="trigger",
           table="sales.Order", timing="AFTER", events=("INSERT", "UPDATE"))
def trOrder_AuditStatus(session: Session, inserted: DataFrame, deleted: DataFrame, updated_columns: frozenset) -> None:
    """Called by the write wrapper after an insert or update on sales.Order.

    `updated_columns` is the set of columns named in the UPDATE's SET list. T-SQL's
    UPDATE(col) asks exactly that, and it cannot be worked out from the rows.
    """
    # src: IF NOT EXISTS (SELECT 1 FROM inserted) RETURN;
    if inserted.isEmpty():
        return

    # src: IF NOT EXISTS (SELECT 1 FROM deleted) BEGIN INSERT ... RETURN; END
    if deleted.isEmpty():
        # Columns not listed (AuditId, ChangedAt, ChangedBy) get their identity and defaults from the wrapper.
        session.insert(
            "audit.OrderAudit",
            inserted.alias("i").select(
                F.col("i.OrderId").alias("OrderId"),
                F.lit("I").alias("Action"),
                F.lit(None).cast("smallint").alias("OldStatus"),
                F.col("i.Status").alias("NewStatus"),
            ),
        )
        return

    # src: IF UPDATE(Status) INSERT ... FROM inserted AS i JOIN deleted AS d ON d.OrderId = i.OrderId WHERE d.Status <> i.Status;
    if "Status" in updated_columns:
        i = inserted.alias("i")
        d = deleted.alias("d")
        session.insert(
            "audit.OrderAudit",
            i.join(d, F.col("d.OrderId") == F.col("i.OrderId"))
            .where(F.col("d.Status") != F.col("i.Status"))
            .select(
                F.col("i.OrderId").alias("OrderId"),
                F.lit("U").alias("Action"),
                F.col("d.Status").alias("OldStatus"),
                F.col("i.Status").alias("NewStatus"),
            ),
        )
