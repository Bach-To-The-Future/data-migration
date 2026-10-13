CREATE TRIGGER ref.trCustomer_SoftDelete
ON ref.Customer
INSTEAD OF DELETE
AS
BEGIN
    SET NOCOUNT ON;

    -- A DELETE never removes a customer. Customers with orders are
    -- deactivated; customers without orders are really deleted.
    UPDATE c
    SET IsActive  = 0,
        DeletedAt = GETDATE()
    FROM ref.Customer AS c
    JOIN deleted AS d
        ON d.CustomerId = c.CustomerId
    WHERE EXISTS (SELECT 1 FROM sales.[Order] AS o WHERE o.CustomerId = c.CustomerId);

    -- Inside an INSTEAD OF DELETE trigger this DELETE does not fire the
    -- trigger again; it performs the real delete.
    DELETE c
    FROM ref.Customer AS c
    JOIN deleted AS d
        ON d.CustomerId = c.CustomerId
    WHERE NOT EXISTS (SELECT 1 FROM sales.[Order] AS o WHERE o.CustomerId = c.CustomerId);
END;
