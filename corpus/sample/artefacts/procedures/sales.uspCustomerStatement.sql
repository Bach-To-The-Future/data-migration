CREATE PROCEDURE sales.uspCustomerStatement
    @CustomerCode varchar(10),
    @Year         int
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @CustomerId int;
    DECLARE @From datetime = DATEFROMPARTS(@Year, 1, 1);
    -- The last instant of the year as datetime: .999 rounds up to the next
    -- year, so legacy code writes .997.
    DECLARE @To   datetime = DATEADD(millisecond, -3, DATEADD(year, 1, @From));

    DECLARE @Lines TABLE
    (
        OrderId   int      NOT NULL,
        OrderDate datetime NOT NULL,
        Amount    money    NOT NULL
    );

    SELECT @CustomerId = CustomerId
    FROM ref.Customer
    WHERE CustomerCode = @CustomerCode;

    IF @CustomerId IS NULL
    BEGIN
        RAISERROR('Customer %s not found.', 11, 1, @CustomerCode);
        RETURN;
    END;

    INSERT INTO @Lines (OrderId, OrderDate, Amount)
    SELECT OrderId, OrderDate, TotalAmount
    FROM sales.[Order]
    WHERE CustomerId = @CustomerId
      AND OrderDate BETWEEN @From AND @To
      AND Status <> 9;

    -- Result set 1: the header.
    SELECT c.CustomerCode,
           c.Name,
           RTRIM(c.Region)                                  AS Region,
           CONVERT(varchar(10), @From, 103)                 AS PeriodFrom,   -- dd/mm/yyyy
           CONVERT(varchar(23), @To, 121)                   AS PeriodTo,     -- yyyy-mm-dd hh:mi:ss.mmm
           (SELECT COUNT(*) FROM @Lines)                    AS Orders,
           c.CreditLimit,
           c.CreditLimit - ISNULL((SELECT SUM(Amount) FROM @Lines), 0) AS CreditRemaining
    FROM ref.Customer AS c
    WHERE c.CustomerId = @CustomerId;

    -- Result set 2: the lines with a running balance.
    SELECT l.OrderId,
           CONVERT(char(8), l.OrderDate, 112)               AS OrderDay,     -- yyyymmdd
           l.Amount,
           SUM(l.Amount) OVER (ORDER BY l.OrderDate, l.OrderId
                               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS RunningTotal,
           CASE WHEN l.Amount >= 1000 THEN 'Large'
                WHEN l.Amount >= 100  THEN 'Medium'
                ELSE 'Small' END                            AS Band
    FROM @Lines AS l
    ORDER BY l.OrderDate, l.OrderId;
END;
