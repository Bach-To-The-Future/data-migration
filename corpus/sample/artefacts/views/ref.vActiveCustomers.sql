CREATE VIEW ref.vActiveCustomers
AS
SELECT c.CustomerId,
       c.CustomerCode,
       c.Name,
       -- CONCAT treats NULL as an empty string; '+' would make the whole value NULL.
       CONCAT(c.Name, ' <', c.Email, '>')                           AS Contact,
       ref.fnFormatCustomerLabel(c.CustomerCode, c.Name, c.Email)   AS Label,
       -- LEN ignores trailing spaces; DATALENGTH does not.
       LEN(c.CustomerCode)                                          AS CodeLength,
       DATALENGTH(c.CustomerCode)                                   AS CodeBytes,
       c.Region,
       c.CreditLimit
FROM ref.Customer AS c
WHERE c.IsActive = 1
  AND c.DeletedAt IS NULL
  -- Region is char(5) and the collation is case-insensitive:
  -- this matches 'EMEA ', 'emea ' and 'Emea'.
  AND c.Region IN ('emea', 'apac');
