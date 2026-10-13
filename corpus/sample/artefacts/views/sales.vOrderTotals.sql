CREATE VIEW sales.vOrderTotals
AS
SELECT o.OrderId,
       o.CustomerId,
       o.OrderDate,
       o.Status,
       COUNT(ol.OrderLineId)                           AS LineCount,
       ISNULL(SUM(ol.Quantity), 0)                     AS Units,
       -- AVG over an int column returns int: (10 + 1) / 2 is 5, not 5.5.
       AVG(ol.Quantity)                                AS AvgUnitsPerLine,
       -- Integer division again; NULL when the order has no lines.
       SUM(ol.Quantity) / NULLIF(COUNT(ol.OrderLineId), 0) AS UnitsPerLine,
       ISNULL(SUM(ol.Quantity * ol.UnitPrice * (100 - ol.DiscountPct) / 100), 0) AS ComputedTotal,
       o.TotalAmount
FROM sales.[Order] AS o
LEFT JOIN sales.OrderLine AS ol
    ON ol.OrderId = o.OrderId
GROUP BY o.OrderId, o.CustomerId, o.OrderDate, o.Status, o.TotalAmount;
