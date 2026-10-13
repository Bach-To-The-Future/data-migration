CREATE FUNCTION sales.fnOrderAgeDays (@OrderDate datetime, @AsOf datetime)
RETURNS int
WITH SCHEMABINDING
AS
BEGIN
    -- DATEDIFF counts day boundaries crossed, not elapsed 24-hour periods:
    -- 23:59 to 00:01 the next day is 1.
    RETURN DATEDIFF(day, @OrderDate, ISNULL(@AsOf, GETDATE()));
END;
