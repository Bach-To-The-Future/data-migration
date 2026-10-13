CREATE PROCEDURE sales.uspRecalculateDailySales
    @FromDate date,
    @ToDate   date
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @Day     date = @FromDate;
    DECLARE @Deleted int  = 0;
    DECLARE @Loaded  int  = 0;

    CREATE TABLE #Sales
    (
        SalesDate date  NOT NULL,
        ProductId int   NOT NULL,
        Units     int   NOT NULL,
        Revenue   money NOT NULL,
        PRIMARY KEY (SalesDate, ProductId)
    );

    -- One day at a time, as the original nightly job did.
    WHILE @Day <= @ToDate
    BEGIN
        INSERT INTO #Sales (SalesDate, ProductId, Units, Revenue)
        SELECT @Day,
               ol.ProductId,
               SUM(ol.Quantity),
               SUM(ol.Quantity * ol.UnitPrice * (100 - ol.DiscountPct) / 100)
        FROM sales.[Order] AS o
        JOIN sales.OrderLine AS ol
            ON ol.OrderId = o.OrderId
        -- An order at 23:59:59.997 belongs to that day; the cast drops the time.
        WHERE CAST(o.OrderDate AS date) = @Day
          AND o.Status <> 9
        GROUP BY ol.ProductId;

        SET @Day = DATEADD(day, 1, @Day);
    END;

    BEGIN TRANSACTION;

    DELETE FROM sales.DailySales
    WHERE SalesDate BETWEEN @FromDate AND @ToDate;

    SET @Deleted = @@ROWCOUNT;

    INSERT INTO sales.DailySales (SalesDate, ProductId, Units, Revenue)
    SELECT SalesDate, ProductId, Units, Revenue
    FROM #Sales;

    SET @Loaded = @@ROWCOUNT;

    COMMIT TRANSACTION;

    DROP TABLE #Sales;

    SELECT @Deleted AS RowsDeleted, @Loaded AS RowsLoaded;
END;
