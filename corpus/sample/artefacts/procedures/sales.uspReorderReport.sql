CREATE PROCEDURE sales.uspReorderReport
    @PackSize int = 12
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @ProductId    int;
    DECLARE @Sku          varchar(20);
    DECLARE @OnHand       int;
    DECLARE @ReorderLevel int;
    DECLARE @Shortfall    int;
    DECLARE @Packs        int;

    CREATE TABLE #Reorder
    (
        Sku        varchar(20) COLLATE DATABASE_DEFAULT NOT NULL,
        OnHand     int         NOT NULL,
        Shortfall  int         NOT NULL,
        Packs      int         NOT NULL,
        Urgency    varchar(10) NOT NULL
    );

    DECLARE products CURSOR LOCAL STATIC FOR
        SELECT p.ProductId, p.Sku, i.OnHand, i.ReorderLevel
        FROM ref.Product AS p
        JOIN sales.Inventory AS i
            ON i.ProductId = p.ProductId
        WHERE p.DiscontinuedOn IS NULL;

    OPEN products;

    WHILE 1 = 1
    BEGIN
        FETCH NEXT FROM products INTO @ProductId, @Sku, @OnHand, @ReorderLevel;
        IF @@FETCH_STATUS <> 0
            BREAK;

        IF @OnHand >= @ReorderLevel
            CONTINUE;

        SET @Shortfall = @ReorderLevel - @OnHand;

        -- Whole packs, rounded up, using integer division: (43 + 11) / 12 = 4.
        SET @Packs = (@Shortfall + @PackSize - 1) / @PackSize;

        INSERT INTO #Reorder (Sku, OnHand, Shortfall, Packs, Urgency)
        VALUES (@Sku, @OnHand, @Shortfall, @Packs,
                CASE WHEN @OnHand = 0 THEN 'OUT'
                     -- Percentage of the reorder level left, truncated.
                     WHEN @OnHand * 100 / @ReorderLevel < 25 THEN 'HIGH'
                     ELSE 'NORMAL' END);
    END;

    CLOSE products;
    DEALLOCATE products;

    SELECT Sku, OnHand, Shortfall, Packs, Urgency
    FROM #Reorder
    ORDER BY CASE Urgency WHEN 'OUT' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END, Sku;
END;
