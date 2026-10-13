CREATE PROCEDURE stg.uspLoadCustomerBatch
    @Code1 varchar(10), @Name1 nvarchar(100),
    @Code2 varchar(10), @Name2 nvarchar(100),
    @Code3 varchar(10), @Name3 nvarchar(100)
AS
BEGIN
    -- Pre-2005 error handling: no TRY/CATCH, XACT_ABORT off, @@ERROR checked
    -- after each statement, GOTO for the exit path. A failed insert is rolled
    -- back on its own and the batch carries on with the next one.
    SET NOCOUNT ON;
    SET XACT_ABORT OFF;

    DECLARE @err    int;
    DECLARE @loaded int;
    DECLARE @failed int;

    SELECT @loaded = 0, @failed = 0;

    BEGIN TRANSACTION;

    INSERT INTO ref.Customer (CustomerCode, Name, Region) VALUES (@Code1, @Name1, 'EMEA');
    SELECT @err = @@ERROR;
    IF @err = 0 SET @loaded = @loaded + 1 ELSE SET @failed = @failed + 1;

    INSERT INTO ref.Customer (CustomerCode, Name, Region) VALUES (@Code2, @Name2, 'EMEA');
    SELECT @err = @@ERROR;
    IF @err = 0 SET @loaded = @loaded + 1 ELSE SET @failed = @failed + 1;

    INSERT INTO ref.Customer (CustomerCode, Name, Region) VALUES (@Code3, @Name3, 'EMEA');
    SELECT @err = @@ERROR;
    IF @err = 0 SET @loaded = @loaded + 1 ELSE SET @failed = @failed + 1;

    IF @loaded = 0
        GOTO nothing_loaded;

    COMMIT TRANSACTION;
    SELECT @loaded AS Loaded, @failed AS Failed;
    RETURN 0;

nothing_loaded:
    ROLLBACK TRANSACTION;
    SELECT 0 AS Loaded, @failed AS Failed;
    RETURN 1;
END;
