CREATE PROCEDURE sales.uspSearchOrders
    @CustomerCode varchar(10)  = NULL,
    @Status       tinyint      = NULL,
    @NoteContains nvarchar(50) = NULL,
    @SortBy       varchar(20)  = 'OrderDate',
    @Top          int          = 50
AS
BEGIN
    SET NOCOUNT ON;

    -- Dynamic SQL: the WHERE clause is assembled from whichever filters were
    -- supplied, then run through sp_executesql with parameters.
    DECLARE @sql    nvarchar(max);
    DECLARE @params nvarchar(200) =
        N'@CustomerCode varchar(10), @Status tinyint, @Pattern nvarchar(52), @Top int';
    DECLARE @Pattern nvarchar(52) = N'%' + @NoteContains + N'%';

    SET @sql = N'
        SELECT TOP (@Top) o.OrderId, c.CustomerCode, o.OrderDate, o.Status, o.TotalAmount, o.Note
        FROM sales.[Order] AS o
        JOIN ref.Customer AS c ON c.CustomerId = o.CustomerId
        WHERE 1 = 1';

    IF @CustomerCode IS NOT NULL
        SET @sql += N' AND c.CustomerCode = @CustomerCode';

    IF @Status IS NOT NULL
        SET @sql += N' AND o.Status = @Status';

    IF @NoteContains IS NOT NULL
        SET @sql += N' AND o.Note LIKE @Pattern';

    -- The sort column cannot be a parameter, so it is whitelisted and quoted.
    SET @sql += N' ORDER BY ' + CASE @SortBy
                                    WHEN 'TotalAmount' THEN N'o.TotalAmount DESC'
                                    WHEN 'Customer'    THEN N'c.CustomerCode, o.OrderDate'
                                    ELSE N'o.OrderDate DESC'
                                END;

    EXEC sys.sp_executesql @sql, @params,
         @CustomerCode = @CustomerCode, @Status = @Status, @Pattern = @Pattern, @Top = @Top;
END;
