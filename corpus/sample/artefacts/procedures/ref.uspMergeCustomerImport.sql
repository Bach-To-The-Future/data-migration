CREATE PROCEDURE ref.uspMergeCustomerImport
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @Merged   int;
    DECLARE @Rejected int;

    -- The staging table carries duplicates that differ only in case or padding
    -- ('ab01', 'AB01  '). PARTITION BY treats them as one code because the
    -- collation is case-insensitive and trailing spaces are ignored.
    ;WITH Ranked AS
    (
        SELECT CustomerCode, Name, Email, Region, CreditLimit,
               ROW_NUMBER() OVER (PARTITION BY CustomerCode ORDER BY LoadedAt DESC, Name DESC) AS rn
        FROM stg.CustomerImport
        WHERE CustomerCode IS NOT NULL
    )
    MERGE ref.Customer AS t
    USING (SELECT CustomerCode, Name, Email, Region, CreditLimit FROM Ranked WHERE rn = 1) AS s
        ON t.CustomerCode = s.CustomerCode
    WHEN MATCHED THEN
        UPDATE SET Name        = s.Name,
                   Email       = ISNULL(s.Email, t.Email),
                   Region      = UPPER(s.Region),
                   -- TRY_CONVERT gives NULL for 'n/a'; money accepts '$1,000'.
                   CreditLimit = ISNULL(TRY_CONVERT(money, s.CreditLimit), t.CreditLimit)
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (CustomerCode, Name, Email, Region, CreditLimit)
        VALUES (UPPER(RTRIM(s.CustomerCode)), s.Name, s.Email, UPPER(s.Region),
                ISNULL(TRY_CONVERT(money, s.CreditLimit), 0));

    SET @Merged = @@ROWCOUNT;

    SELECT @Rejected = COUNT(*)
    FROM stg.CustomerImport
    WHERE CustomerCode IS NULL;

    DELETE FROM stg.CustomerImport
    WHERE CustomerCode IS NOT NULL;

    SELECT @Merged AS CustomersMerged, @Rejected AS RowsRejected, @@ROWCOUNT AS StagingRowsCleared;
END;
