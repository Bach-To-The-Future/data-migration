-- Month-end adjustments. Run by hand from a query window, top to bottom.
-- GO separates batches: variables do not survive it.

SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

DECLARE @MonthEnd datetime = '2024-03-31T23:59:59.997';
DECLARE @Touched  int;

BEGIN TRANSACTION;

-- 1. Orders shipped but never closed in the month become closed.
UPDATE sales.[Order]
SET Status = 2
WHERE Status = 1
  AND ShippedAt <= @MonthEnd;

SET @Touched = @@ROWCOUNT;

-- 2. Loyalty: raise the credit limit of customers who spent over 100 this
--    quarter. The quarter is counted in boundaries crossed, not elapsed time.
UPDATE c
SET CreditLimit = c.CreditLimit * 1.10
FROM ref.Customer AS c
JOIN (SELECT o.CustomerId, SUM(o.TotalAmount) AS Spent
      FROM sales.[Order] AS o
      WHERE DATEDIFF(quarter, o.OrderDate, @MonthEnd) = 0
        AND o.Status <> 9
      GROUP BY o.CustomerId
      HAVING SUM(o.TotalAmount) > 100) AS s
    ON s.CustomerId = c.CustomerId
WHERE c.IsActive = 1;

SET @Touched = @Touched + @@ROWCOUNT;

COMMIT TRANSACTION;

PRINT 'Rows adjusted: ' + CAST(@Touched AS varchar(10));
GO

-- 3. Tidy blank notes. An empty string and a string of spaces are equal here.
UPDATE sales.[Order]
SET Note = NULL
WHERE Note = '';
GO
