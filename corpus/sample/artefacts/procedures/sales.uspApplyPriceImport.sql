CREATE PROCEDURE sales.uspApplyPriceImport
    @RemoveMissing bit = 0
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @Changes TABLE
    (
        Action   nvarchar(10) NOT NULL,
        Sku      varchar(20)  NOT NULL,
        OldPrice money        NULL,
        NewPrice money        NULL
    );

    -- ref.Product.Sku is case-sensitive, so the staged 'wid-002  ' is trimmed
    -- and upper-cased first. Without that it would be inserted as a new product.
    MERGE ref.Product AS t
    USING (SELECT UPPER(RTRIM(Sku)) AS Sku, UnitPrice, Category
           FROM stg.ProductPriceImport) AS s
        ON t.Sku = s.Sku COLLATE Latin1_General_CS_AS
    WHEN MATCHED AND t.UnitPrice <> s.UnitPrice THEN
        UPDATE SET UnitPrice = s.UnitPrice,
                   Category  = ISNULL(s.Category, t.Category)
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (Sku, Name, Category, UnitPrice)
        VALUES (s.Sku, N'(new) ' + s.Sku, ISNULL(s.Category, 'Unclassified'), s.UnitPrice)
    WHEN NOT MATCHED BY SOURCE AND @RemoveMissing = 1 AND t.DiscontinuedOn IS NULL THEN
        UPDATE SET DiscontinuedOn = CAST(GETDATE() AS date)
    OUTPUT $action, inserted.Sku, deleted.UnitPrice, inserted.UnitPrice
        INTO @Changes (Action, Sku, OldPrice, NewPrice);

    SELECT Action,
           COUNT(*) AS Products,
           -- Percentage change; money / money keeps four decimal places.
           AVG((NewPrice - OldPrice) / NULLIF(OldPrice, 0) * 100) AS AvgPctChange
    FROM @Changes
    GROUP BY Action;
END;
