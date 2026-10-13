CREATE FUNCTION ref.fnSplitCodes (@List varchar(8000), @Delimiter char(1))
RETURNS @Codes TABLE
(
    Position int         NOT NULL,
    Code     varchar(20) NOT NULL
)
AS
BEGIN
    -- Written before STRING_SPLIT existed: a loop over CHARINDEX.
    DECLARE @start int = 1;
    DECLARE @end   int;
    DECLARE @pos   int = 0;
    DECLARE @item  varchar(8000);

    IF @List IS NULL OR LEN(@List) = 0
        RETURN;

    WHILE @start <= LEN(@List) + 1
    BEGIN
        SET @end = CHARINDEX(@Delimiter, @List, @start);
        IF @end = 0
            SET @end = LEN(@List) + 1;

        SET @item = LTRIM(RTRIM(SUBSTRING(@List, @start, @end - @start)));
        SET @pos += 1;

        IF @item <> ''
            INSERT INTO @Codes (Position, Code) VALUES (@pos, LEFT(@item, 20));

        SET @start = @end + 1;
    END;

    RETURN;
END;
