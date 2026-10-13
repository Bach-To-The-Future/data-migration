CREATE PROCEDURE sales.uspCloseStaleOrders
    @AsOf        datetime,
    @StaleAfter  int = 30,
    @DryRun      bit = 0
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @OrderId   int;
    DECLARE @OrderDate datetime;
    DECLARE @Status    tinyint;
    DECLARE @Lines     int;
    DECLARE @Closed    int = 0;
    DECLARE @Cancelled int = 0;

    -- Row by row, because each order gets a different treatment.
    DECLARE stale_orders CURSOR LOCAL FAST_FORWARD FOR
        SELECT o.OrderId, o.OrderDate, o.Status
        FROM sales.[Order] AS o
        WHERE o.Status IN (0, 1)
          AND DATEDIFF(day, o.OrderDate, @AsOf) > @StaleAfter
        ORDER BY o.OrderDate;

    OPEN stale_orders;
    FETCH NEXT FROM stale_orders INTO @OrderId, @OrderDate, @Status;

    WHILE @@FETCH_STATUS = 0
    BEGIN
        SELECT @Lines = COUNT(*) FROM sales.OrderLine WHERE OrderId = @OrderId;

        IF @Lines = 0
        BEGIN
            -- An order with no lines was never real: cancel it.
            IF @DryRun = 0
                UPDATE sales.[Order]
                SET Status = 9,
                    Note   = ISNULL(Note + N' ', N'') + N'[auto-cancelled: empty]'
                WHERE OrderId = @OrderId;

            SET @Cancelled += 1;
        END
        ELSE IF @Status = 1
        BEGIN
            IF @DryRun = 0
                UPDATE sales.[Order] SET Status = 2 WHERE OrderId = @OrderId;

            SET @Closed += 1;
        END
        ELSE
            PRINT 'Order ' + CAST(@OrderId AS varchar(12)) + ' is stale but not shipped; left open.';

        FETCH NEXT FROM stale_orders INTO @OrderId, @OrderDate, @Status;
    END;

    CLOSE stale_orders;
    DEALLOCATE stale_orders;

    SELECT @Closed AS OrdersClosed, @Cancelled AS OrdersCancelled;
END;
