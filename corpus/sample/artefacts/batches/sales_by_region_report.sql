-- Ad hoc report: revenue by region and product category, with subtotals.

DECLARE @From date = '2024-01-01';
DECLARE @To   date = '2024-03-31';

SELECT ISNULL(UPPER(RTRIM(c.Region)), '(all regions)')      AS Region,
       ISNULL(p.Category, '(all categories)')               AS Category,
       COUNT(DISTINCT o.OrderId)                            AS Orders,
       SUM(ol.Quantity)                                     AS Units,
       SUM(ol.Quantity * ol.UnitPrice)                      AS Revenue,
       -- Whole currency units per order: money to int truncates, it does not round.
       CAST(SUM(ol.Quantity * ol.UnitPrice) AS int) / COUNT(DISTINCT o.OrderId) AS WholeRevenuePerOrder,
       -- Share of all units, as a whole percentage.
       SUM(ol.Quantity) * 100 / SUM(SUM(ol.Quantity)) OVER (PARTITION BY GROUPING(c.Region), GROUPING(p.Category)) AS PctOfUnits
FROM sales.[Order] AS o
JOIN ref.Customer AS c
    ON c.CustomerId = o.CustomerId
JOIN sales.OrderLine AS ol
    ON ol.OrderId = o.OrderId
JOIN ref.Product AS p
    ON p.ProductId = ol.ProductId
WHERE o.OrderDate >= @From
  AND o.OrderDate <  DATEADD(day, 1, @To)
  AND o.Status <> 9
-- Region is char(5) and case-insensitive: 'EMEA ' and 'emea ' are one group,
-- and the value shown is whichever spelling the engine happens to keep.
GROUP BY ROLLUP (c.Region, p.Category)
ORDER BY GROUPING(c.Region), Region, GROUPING(p.Category), Category;
