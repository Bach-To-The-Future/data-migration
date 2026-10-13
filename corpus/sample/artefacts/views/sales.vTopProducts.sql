CREATE VIEW sales.vTopProducts
AS
-- WITH TIES returns every product tied with the third place, so this can
-- return more than three rows.
SELECT TOP (3) WITH TIES
       p.ProductId,
       p.Sku,
       p.Name,
       SUM(ol.Quantity)                                       AS Units,
       RANK() OVER (ORDER BY SUM(ol.Quantity) DESC)           AS UnitsRank,
       SUM(ol.Quantity) * 100 / SUM(SUM(ol.Quantity)) OVER () AS PctOfUnits
FROM ref.Product AS p
JOIN sales.OrderLine AS ol
    ON ol.ProductId = p.ProductId
GROUP BY p.ProductId, p.Sku, p.Name
ORDER BY SUM(ol.Quantity) DESC;
