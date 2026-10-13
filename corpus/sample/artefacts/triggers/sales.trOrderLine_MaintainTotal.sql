CREATE TRIGGER sales.trOrderLine_MaintainTotal
ON sales.OrderLine
AFTER INSERT, UPDATE, DELETE
AS
BEGIN
    SET NOCOUNT ON;

    -- Keeps sales.[Order].TotalAmount in step with its lines. The affected
    -- orders are the union of both pseudo-tables: a DELETE has no inserted
    -- rows, an INSERT has no deleted rows, and an UPDATE may move a line
    -- from one order to another.
    ;WITH Affected AS
    (
        SELECT OrderId FROM inserted
        UNION
        SELECT OrderId FROM deleted
    )
    UPDATE o
    SET TotalAmount = ISNULL(t.Total, 0)
    FROM sales.[Order] AS o
    JOIN Affected AS a
        ON a.OrderId = o.OrderId
    LEFT JOIN (SELECT ol.OrderId,
                      SUM(ol.Quantity * ol.UnitPrice * (100 - ol.DiscountPct) / 100) AS Total
               FROM sales.OrderLine AS ol
               GROUP BY ol.OrderId) AS t
        ON t.OrderId = o.OrderId;
END;
