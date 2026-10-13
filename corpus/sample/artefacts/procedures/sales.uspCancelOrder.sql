CREATE PROCEDURE sales.uspCancelOrder
    @OrderId int,
    @Reason  nvarchar(100) = NULL
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @rc         int;
    DECLARE @StartTrans int = @@TRANCOUNT;

    -- Callable on its own or from inside a caller's transaction: only the
    -- outermost level commits or rolls back.
    IF @StartTrans = 0
        BEGIN TRANSACTION;
    ELSE
        SAVE TRANSACTION cancel_order;

    BEGIN TRY
        EXEC @rc = sales.uspUpdateOrderStatus @OrderId = @OrderId, @NewStatus = 9;

        IF @rc <> 0
        BEGIN
            IF @StartTrans = 0 ROLLBACK TRANSACTION; ELSE ROLLBACK TRANSACTION cancel_order;
            RETURN @rc;
        END;

        -- Put the stock back.
        UPDATE i
        SET OnHand = i.OnHand + x.Quantity
        FROM sales.Inventory AS i
        JOIN (SELECT ProductId, SUM(Quantity) AS Quantity
              FROM sales.OrderLine
              WHERE OrderId = @OrderId
              GROUP BY ProductId) AS x
            ON x.ProductId = i.ProductId;

        UPDATE sales.[Order]
        SET Note = LEFT(ISNULL(RTRIM(Note) + N'; ', N'') + N'Cancelled: ' + ISNULL(@Reason, N'no reason given'), 200)
        WHERE OrderId = @OrderId;

        IF @StartTrans = 0
            COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF @StartTrans = 0 AND @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;
        ELSE IF @StartTrans > 0 AND XACT_STATE() = 1
            ROLLBACK TRANSACTION cancel_order;

        EXEC audit.uspLogError @ProcName = 'sales.uspCancelOrder';
        THROW;
    END CATCH;

    RETURN 0;
END;
