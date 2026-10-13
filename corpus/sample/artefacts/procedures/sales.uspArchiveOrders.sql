CREATE PROCEDURE sales.uspArchiveOrders
    @OlderThan datetime,
    @BatchSize int = 2
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @Moved   int = 0;
    DECLARE @Batch   int = 1;
    DECLARE @Batches int = 0;
    DECLARE @Ids     TABLE (OrderId int NOT NULL PRIMARY KEY);

    -- Small batches keep the transaction log and lock footprint down.
    WHILE @Batch > 0
    BEGIN
        DELETE FROM @Ids;

        BEGIN TRANSACTION;

        INSERT INTO @Ids (OrderId)
        SELECT TOP (@BatchSize) OrderId
        FROM sales.[Order]
        WHERE Status = 2
          AND OrderDate < @OlderThan
        ORDER BY OrderDate;

        SET @Batch = @@ROWCOUNT;

        IF @Batch = 0
        BEGIN
            COMMIT TRANSACTION;
            BREAK;
        END;

        SAVE TRANSACTION before_lines;

        BEGIN TRY
            DELETE ol
            FROM sales.OrderLine AS ol
            JOIN @Ids AS i
                ON i.OrderId = ol.OrderId;

            -- OUTPUT moves the deleted rows into the archive in the same statement.
            DELETE o
            OUTPUT deleted.OrderId, deleted.CustomerId, deleted.OrderDate,
                   deleted.Status, deleted.TotalAmount, GETDATE()
                INTO sales.OrderArchive (OrderId, CustomerId, OrderDate, Status, TotalAmount, ArchivedAt)
            FROM sales.[Order] AS o
            JOIN @Ids AS i
                ON i.OrderId = o.OrderId;

            SET @Moved += @@ROWCOUNT;
            SET @Batches += 1;
        END TRY
        BEGIN CATCH
            -- Undo this batch only; batches already committed stay archived.
            IF XACT_STATE() = 1
                ROLLBACK TRANSACTION before_lines;

            EXEC audit.uspLogError @ProcName = 'sales.uspArchiveOrders';
            SET @Batch = 0;
        END CATCH;

        IF @@TRANCOUNT > 0
            COMMIT TRANSACTION;
    END;

    SELECT @Moved AS OrdersArchived, @Batches AS Batches;
END;
