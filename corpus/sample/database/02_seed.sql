-- Seed rows. Small, and deliberately awkward: nulls, mixed case, trailing
-- spaces, boundary dates, datetime values that round, and duplicates in staging.

SET NOCOUNT ON;

INSERT INTO ref.Customer (CustomerCode, Name, Email, Region, CreditLimit, IsActive, CreatedAt)
VALUES
    ('AB01',   N'Alpha Bakery',     'orders@alpha.example',   'EMEA',  5000.00,  1, '2019-03-01T08:15:00'),
    ('cd02',   N'Cedar Dental',     NULL,                     'emea',  1200.50,  1, '2020-11-30T23:59:59.997'),
    ('EF03  ', N'Elm Freight',      'ops@elm.example',        'APAC',  0,        1, '2021-01-01T00:00:00'),
    ('GH04',   N'Grove Hotels',     'ap@grove.example  ',     'AMER',  25000.00, 0, '1900-01-01T00:00:00'),
    ('ij05',   N'Íris Jóns ehf.',   'iris@jons.example',      'EMEA',  750.00,   1, '2022-02-28T12:00:00.003'),
    ('KL06',   N'Kestrel Labs',     '',                       'APAC',  100.00,   1, '2023-12-31T23:59:59.990');

INSERT INTO ref.Product (Sku, Name, Category, UnitPrice, WeightKg, DiscontinuedOn)
VALUES
    ('WID-001', N'Widget, small',   'Widgets',   2.50,    0.125, NULL),
    ('wid-001', N'Widget, sample',  'Widgets',   0.00,    0.125, NULL),          -- differs from WID-001 only by case
    ('WID-002', N'Widget, large',   'Widgets',   7.99,    1.500, NULL),
    ('GAD-100', N'Gadget',          'Gadgets',   19.9999, NULL,  NULL),
    ('GAD-200', N'Gadget, deluxe',  'gadgets',   149.00,  3.250, '2024-06-30'),
    ('SRV-INS', N'Installation',    'Services',  85.00,   NULL,  NULL),
    ('SRV-SUP', N'Support, annual', 'Services',  1200.00, NULL,  NULL);

INSERT INTO sales.Inventory (ProductId, OnHand, ReorderLevel)
SELECT ProductId,
       CASE Sku WHEN 'WID-001' THEN 500 WHEN 'WID-002' THEN 7 WHEN 'GAD-100' THEN 40 WHEN 'GAD-200' THEN 0 ELSE 0 END,
       CASE Category WHEN 'Widgets' THEN 50 WHEN 'Gadgets' THEN 10 ELSE 0 END
FROM ref.Product
WHERE Category <> 'Services';

DECLARE @c1 int = (SELECT CustomerId FROM ref.Customer WHERE CustomerCode = 'AB01');
DECLARE @c2 int = (SELECT CustomerId FROM ref.Customer WHERE CustomerCode = 'CD02');
DECLARE @c3 int = (SELECT CustomerId FROM ref.Customer WHERE CustomerCode = 'EF03');
DECLARE @c5 int = (SELECT CustomerId FROM ref.Customer WHERE CustomerCode = 'IJ05');

INSERT INTO sales.[Order] (CustomerId, OrderDate, ShippedAt, Status, Note)
VALUES
    (@c1, '2024-01-15T09:30:00',     '2024-01-16T14:00:00.1234567', 2, N'First order'),
    (@c1, '2024-01-31T23:59:59.997', '2024-02-01T00:00:00.0000001', 1, NULL),
    (@c2, '2024-02-29T12:00:00',     NULL,                          0, N''),
    (@c2, '2023-12-31T23:59:59.997', '2024-01-01T00:00:00',         2, N'Year boundary'),
    (@c3, '2024-03-10T00:00:00',     NULL,                          9, N'Cancelled by customer'),
    (@c5, '2022-06-01T08:00:00',     '2022-06-03T08:00:00',         2, NULL),
    (@c5, '2024-03-31T18:45:10.123', NULL,                          0, N'Rush  ');

INSERT INTO sales.OrderLine (OrderId, ProductId, Quantity, UnitPrice, DiscountPct)
SELECT o.OrderId, p.ProductId, v.Quantity, p.UnitPrice, v.DiscountPct
FROM (VALUES
        (1, 'WID-001', 10, 0),  (1, 'GAD-100', 1, 10),
        (2, 'WID-002', 3,  0),  (2, 'SRV-INS', 1, 0),  (2, 'WID-001', 7, 5),
        (3, 'GAD-100', 2,  0),
        (4, 'SRV-SUP', 1,  15), (4, 'GAD-200', 1, 0),
        (5, 'WID-001', 100, 0),
        (6, 'WID-002', 1,  0),
        (7, 'GAD-100', 5,  33), (7, 'WID-001', 1, 0)
     ) AS v (OrderNo, Sku, Quantity, DiscountPct)
JOIN (SELECT OrderId, ROW_NUMBER() OVER (ORDER BY OrderId) AS OrderNo FROM sales.[Order]) AS o
    ON o.OrderNo = v.OrderNo
JOIN ref.Product AS p
    ON p.Sku = v.Sku;

UPDATE o
SET TotalAmount = t.Total
FROM sales.[Order] AS o
JOIN (SELECT OrderId, SUM(Quantity * UnitPrice * (100 - DiscountPct) / 100) AS Total
      FROM sales.OrderLine
      GROUP BY OrderId) AS t
    ON t.OrderId = o.OrderId;

-- Staging rows as an upstream system would send them: inconsistent case,
-- padding, a duplicate, a null key and a non-numeric amount.
INSERT INTO stg.CustomerImport (CustomerCode, Name, Email, Region, CreditLimit)
VALUES
    ('ab01',    N'Alpha Bakery Ltd',  'orders@alpha.example', 'EMEA',   '6000'),
    ('AB01  ',  N'Alpha Bakery Ltd.', 'orders@alpha.example', 'EMEA',   '6500.00'),
    ('MN07',    N'Maple Networks',    NULL,                   'amer',   '300'),
    ('mn07',    N'Maple Networks',    'it@maple.example',     'AMER',   'n/a'),
    ('OP08',    N'Opal Printing',     'hello@opal.example',   'APAC  ', '$1,000'),
    (NULL,      N'No Code Co',        NULL,                   'EMEA',   '10');

INSERT INTO stg.ProductPriceImport (Sku, UnitPrice, Category)
VALUES
    ('WID-001',   2.75,  'Widgets'),
    ('wid-002  ', 8.49,  'widgets'),
    ('GAD-300',   59.00, 'Gadgets'),
    ('SRV-INS',   85.00, NULL);
