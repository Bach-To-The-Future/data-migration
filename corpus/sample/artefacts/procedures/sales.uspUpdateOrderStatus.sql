CREATE PROCEDURE sales.uspUpdateOrderStatus
    @OrderId   int,
    @NewStatus tinyint
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @Changed TABLE (OrderId int, OldStatus tinyint, NewStatus tinyint);

    IF @NewStatus NOT IN (0, 1, 2, 9)
    BEGIN
        RAISERROR('Status %d is not valid.', 16, 1, @NewStatus);
        RETURN 1;
    END;

    -- Closed and cancelled orders are final. UPDATE ... FROM with a join is
    -- T-SQL's own syntax; the audit trigger on sales.[Order] records the change.
    UPDATE o
    SET Status    = @NewStatus,
        ShippedAt = CASE WHEN @NewStatus = 1 AND o.ShippedAt IS NULL THEN SYSDATETIME() ELSE o.ShippedAt END
    OUTPUT inserted.OrderId, deleted.Status, inserted.Status INTO @Changed
    FROM sales.[Order] AS o
    JOIN ref.Customer AS c
        ON c.CustomerId = o.CustomerId
    WHERE o.OrderId = @OrderId
      AND o.Status NOT IN (2, 9)
      AND o.Status <> @NewStatus
      AND c.IsActive = 1;

    IF @@ROWCOUNT = 0
    BEGIN
        -- Severity 10 is informational: it does not transfer control to a CATCH block.
        RAISERROR('Order %d was not changed.', 10, 1, @OrderId);
        RETURN 2;
    END;

    SELECT OrderId, OldStatus, NewStatus FROM @Changed;
    RETURN 0;
END;
