CREATE PROCEDURE sales.uspGetOrdersByCustomer
    @CustomerCode varchar(10),
    @FromDate     datetime = NULL,
    @ToDate       datetime = NULL
AS
BEGIN
    SET NOCOUNT ON;

    -- The code is matched case-insensitively and ignoring trailing spaces,
    -- so 'ab01', 'AB01' and 'AB01  ' all find the same customer.
    SELECT o.OrderId,
           o.OrderDate,
           o.Status,
           o.TotalAmount,
           sales.fnOrderAgeDays(o.OrderDate, NULL) AS AgeDays
    FROM sales.[Order] AS o
    JOIN ref.Customer AS c
        ON c.CustomerId = o.CustomerId
    WHERE c.CustomerCode = @CustomerCode
      AND o.OrderDate >= ISNULL(@FromDate, '19000101')
      AND o.OrderDate <  ISNULL(@ToDate, '99991231')
    ORDER BY o.OrderDate DESC;
END;
