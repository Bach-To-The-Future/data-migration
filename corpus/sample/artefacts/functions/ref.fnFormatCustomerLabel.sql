CREATE FUNCTION ref.fnFormatCustomerLabel
(
    @CustomerCode varchar(10),
    @Name         nvarchar(100),
    @Email        varchar(100)
)
RETURNS nvarchar(200)
AS
BEGIN
    DECLARE @label nvarchar(200);
    DECLARE @domain varchar(100);
    DECLARE @at int = CHARINDEX('@', @Email);

    -- CHARINDEX returns 0, not NULL, when there is no '@'.
    SET @domain = CASE WHEN @at > 0 THEN SUBSTRING(@Email, @at + 1, LEN(@Email)) ELSE '' END;

    -- '+' yields NULL when any part is NULL, so ISNULL guards the optional piece.
    SET @label = UPPER(RTRIM(@CustomerCode)) + N' - ' + @Name
                 + ISNULL(N' (' + NULLIF(@domain, '') + N')', N'');

    -- Mask the middle of long codes: STUFF with a start past the end returns NULL.
    IF LEN(@CustomerCode) > 6
        SET @label = ISNULL(STUFF(@label, 3, 2, '**'), @label);

    RETURN @label;
END;
