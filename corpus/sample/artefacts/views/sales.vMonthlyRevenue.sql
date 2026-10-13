CREATE VIEW sales.vMonthlyRevenue
AS
SELECT YEAR(o.OrderDate)                                   AS OrderYear,
       MONTH(o.OrderDate)                                  AS OrderMonth,
       DATEPART(week, o.OrderDate)                         AS OrderWeek,
       -- Category is compared case-insensitively, so 'Gadgets' and 'gadgets' are one group.
       p.Category,
       COUNT(DISTINCT o.OrderId)                           AS Orders,
       SUM(ol.Quantity)                                    AS Units,
       SUM(ol.Quantity * ol.UnitPrice)                     AS GrossRevenue,
       -- money divided by int stays money and keeps four decimal places.
       SUM(ol.Quantity * ol.UnitPrice) / COUNT(DISTINCT o.OrderId) AS RevenuePerOrder,
       MIN(DATEDIFF(day, o.OrderDate, o.ShippedAt))        AS FastestShipDays
FROM sales.[Order] AS o
JOIN sales.OrderLine AS ol
    ON ol.OrderId = o.OrderId
JOIN ref.Product AS p
    ON p.ProductId = ol.ProductId
WHERE o.Status <> 9
GROUP BY YEAR(o.OrderDate), MONTH(o.OrderDate), DATEPART(week, o.OrderDate), p.Category;
