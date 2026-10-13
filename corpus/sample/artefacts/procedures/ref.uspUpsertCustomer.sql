CREATE PROCEDURE ref.uspUpsertCustomer
    @CustomerCode varchar(10),
    @Name         nvarchar(100),
    @Email        varchar(100) = NULL,
    @Region       char(5),
    @CustomerId   int OUTPUT
AS
BEGIN
    SET NOCOUNT ON;

    -- The classic upsert: try the insert, and on a duplicate key update instead.
    -- It depends on the unique constraint being enforced.
    BEGIN TRY
        INSERT INTO ref.Customer (CustomerCode, Name, Email, Region)
        VALUES (@CustomerCode, @Name, @Email, @Region);

        SET @CustomerId = SCOPE_IDENTITY();
    END TRY
    BEGIN CATCH
        IF ERROR_NUMBER() NOT IN (2601, 2627)
        BEGIN
            EXEC audit.uspLogError @ProcName = 'ref.uspUpsertCustomer';
            THROW;
        END;

        UPDATE ref.Customer
        SET Name   = @Name,
            Email  = ISNULL(@Email, Email),
            Region = @Region
        WHERE CustomerCode = @CustomerCode;

        SELECT @CustomerId = CustomerId
        FROM ref.Customer
        WHERE CustomerCode = @CustomerCode;
    END CATCH;
END;
