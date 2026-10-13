CREATE PROCEDURE sales.uspTransferStock
    @FromSku  varchar(20),
    @ToSku    varchar(20),
    @Quantity int
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @FromId int, @ToId int, @rc int;

    IF @Quantity IS NULL OR @Quantity <= 0
        RETURN -1;

    SELECT @FromId = ProductId FROM ref.Product WHERE Sku = @FromSku;
    SELECT @ToId   = ProductId FROM ref.Product WHERE Sku = @ToSku;

    IF @FromId IS NULL OR @ToId IS NULL
        RETURN -2;

    BEGIN TRANSACTION;

    UPDATE sales.Inventory
    SET OnHand = OnHand - @Quantity
    WHERE ProductId = @FromId
      AND OnHand >= @Quantity;

    -- @@ROWCOUNT must be read immediately: any statement, even SET, resets it.
    SET @rc = @@ROWCOUNT;

    IF @rc = 0
    BEGIN
        ROLLBACK TRANSACTION;
        RETURN -3; -- not enough stock, or no inventory row
    END;

    UPDATE sales.Inventory
    SET OnHand = OnHand + @Quantity
    WHERE ProductId = @ToId;

    IF @@ROWCOUNT = 0
        INSERT INTO sales.Inventory (ProductId, OnHand, ReorderLevel)
        VALUES (@ToId, @Quantity, 0);

    COMMIT TRANSACTION;
    RETURN 0;
END;
