CREATE PROCEDURE sales.uspCreateOrder
    @CustomerCode varchar(10),
    @Lines        sales.OrderLineInput READONLY,
    @Note         nvarchar(200) = NULL,
    @OrderId      int OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @CustomerId int;
    DECLARE @LineCount  int;

    SELECT @CustomerId = CustomerId
    FROM ref.Customer
    WHERE CustomerCode = @CustomerCode
      AND IsActive = 1;

    IF @CustomerId IS NULL
    BEGIN
        RAISERROR('Unknown or inactive customer %s.', 16, 1, @CustomerCode);
        RETURN 1;
    END;

    BEGIN TRY
        BEGIN TRANSACTION;

        INSERT INTO sales.[Order] (CustomerId, OrderDate, Note)
        VALUES (@CustomerId, GETDATE(), @Note);

        -- SCOPE_IDENTITY, not @@IDENTITY: the audit trigger on sales.[Order]
        -- inserts into a table with its own identity column.
        SET @OrderId = SCOPE_IDENTITY();

        -- Sku is case-sensitive, so 'wid-001' and 'WID-001' are different products here.
        INSERT INTO sales.OrderLine (OrderId, ProductId, Quantity, UnitPrice, DiscountPct)
        SELECT @OrderId, p.ProductId, l.Quantity, p.UnitPrice, l.DiscountPct
        FROM @Lines AS l
        JOIN ref.Product AS p
            ON p.Sku = l.Sku COLLATE Latin1_General_CS_AS
        WHERE p.DiscontinuedOn IS NULL;

        SET @LineCount = @@ROWCOUNT;

        IF @LineCount <> (SELECT COUNT(*) FROM @Lines)
            THROW 50001, 'One or more lines refer to an unknown or discontinued product.', 1;

        UPDATE i
        SET OnHand = i.OnHand - l.Quantity
        FROM sales.Inventory AS i
        JOIN ref.Product AS p
            ON p.ProductId = i.ProductId
        JOIN @Lines AS l
            ON l.Sku COLLATE Latin1_General_CS_AS = p.Sku;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;

        EXEC audit.uspLogError @ProcName = 'sales.uspCreateOrder';
        SET @OrderId = NULL;
        THROW;
    END CATCH;

    RETURN 0;
END;
