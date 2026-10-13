CREATE PROCEDURE stg.uspPurgeStaging
    @Like sysname = '%'
AS
BEGIN
    SET NOCOUNT ON;

    -- Dynamic SQL of the other kind: a statement built by concatenation and
    -- run with EXEC(), once per staging table found in the catalog views.
    DECLARE @table sysname;
    DECLARE @sql   nvarchar(400);
    DECLARE @count int = 0;

    DECLARE staging_tables CURSOR LOCAL FAST_FORWARD FOR
        SELECT t.name
        FROM sys.tables AS t
        JOIN sys.schemas AS s
            ON s.schema_id = t.schema_id
        WHERE s.name = 'stg'
          AND t.name LIKE @Like;

    OPEN staging_tables;
    FETCH NEXT FROM staging_tables INTO @table;

    WHILE @@FETCH_STATUS = 0
    BEGIN
        SET @sql = 'TRUNCATE TABLE stg.' + QUOTENAME(@table) + ';';
        EXEC (@sql);
        SET @count += 1;

        FETCH NEXT FROM staging_tables INTO @table;
    END;

    CLOSE staging_tables;
    DEALLOCATE staging_tables;

    RETURN @count;
END;
