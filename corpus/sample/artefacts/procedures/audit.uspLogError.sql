CREATE PROCEDURE audit.uspLogError
    @ProcName sysname
AS
BEGIN
    SET NOCOUNT ON;

    -- Called from CATCH blocks. If the caller's transaction is doomed,
    -- nothing can be written until it is rolled back.
    IF XACT_STATE() = -1
        ROLLBACK TRANSACTION;

    INSERT INTO audit.ErrorLog (ProcName, ErrorNumber, ErrorMessage)
    VALUES (@ProcName, ERROR_NUMBER(), ERROR_MESSAGE());
END;
