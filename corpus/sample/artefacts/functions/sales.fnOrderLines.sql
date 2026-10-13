CREATE FUNCTION sales.fnOrderLines (@OrderId int)
RETURNS TABLE
AS
RETURN
(
    SELECT ol.OrderLineId,
           p.Sku,
           p.Name                                                     AS ProductName,
           ol.Quantity,
           ol.UnitPrice,
           ol.DiscountPct,
           ol.Quantity * ol.UnitPrice * (100 - ol.DiscountPct) / 100  AS LineTotal
    FROM sales.OrderLine AS ol
    JOIN ref.Product AS p
        ON p.ProductId = ol.ProductId
    WHERE ol.OrderId = @OrderId
);
