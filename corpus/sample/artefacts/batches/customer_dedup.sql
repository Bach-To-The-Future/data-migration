-- Remove duplicate staged customers, keeping the newest row for each code.
-- Codes that differ only in case or trailing spaces count as the same code.

WITH Ranked AS
(
    SELECT CustomerCode,
           ROW_NUMBER() OVER (PARTITION BY CustomerCode
                              ORDER BY LoadedAt DESC, LEN(Name) DESC) AS rn
    FROM stg.CustomerImport
    WHERE CustomerCode IS NOT NULL
)
DELETE FROM Ranked
WHERE rn > 1;

-- How many distinct customers are left, and how many rows have no code.
SELECT COUNT(DISTINCT CustomerCode) AS DistinctCodes,
       SUM(CASE WHEN CustomerCode IS NULL THEN 1 ELSE 0 END) AS RowsWithoutCode,
       COUNT(*) AS RowsRemaining
FROM stg.CustomerImport;
