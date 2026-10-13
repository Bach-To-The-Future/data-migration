-- Reload the price staging table from last quarter's orders, then apply it.

TRUNCATE TABLE stg.ProductPriceImport;

INSERT INTO stg.ProductPriceImport (Sku, UnitPrice, Category)
SELECT p.Sku,
       -- Average price actually charged, rounded half away from zero to cents.
       ROUND(AVG(ol.UnitPrice * (100 - ol.DiscountPct) / 100), 2),
       p.Category
FROM ref.Product AS p
JOIN sales.OrderLine AS ol
    ON ol.ProductId = p.ProductId
JOIN sales.[Order] AS o
    ON o.OrderId = ol.OrderId
WHERE o.OrderDate >= DATEADD(quarter, -1, '2024-04-01')
  AND p.DiscontinuedOn IS NULL
GROUP BY p.Sku, p.Category
HAVING COUNT(*) > 0;

IF @@ROWCOUNT = 0
    PRINT 'No prices staged; nothing to apply.';
ELSE
    EXEC sales.uspApplyPriceImport @RemoveMissing = 0;
