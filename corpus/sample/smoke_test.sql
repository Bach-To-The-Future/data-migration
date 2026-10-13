-- Runs every sample view, function, procedure and trigger once, on a scratch
-- copy of the sample database, and fails loudly where a result is not what
-- the artefact's comments promise. corpus/build.py runs it on every build.

SET NOCOUNT ON;
GO

-- Views and functions -------------------------------------------------------

DECLARE @n int;

SELECT @n = COUNT(*) FROM ref.vActiveCustomers;
IF @n <> 5 THROW 51000, 'vActiveCustomers: expected 5 active EMEA/APAC customers (case-insensitive match).', 1;

IF (SELECT AvgUnitsPerLine FROM sales.vOrderTotals WHERE OrderId = 1) <> 5
    THROW 51001, 'vOrderTotals: AVG over int should truncate (10 + 1) / 2 to 5.', 1;

SELECT @n = COUNT(*) FROM sales.vMonthlyRevenue;
IF @n = 0 THROW 51002, 'vMonthlyRevenue returned no rows.', 1;

SELECT @n = COUNT(*) FROM sales.vTopProducts;
IF @n < 3 THROW 51003, 'vTopProducts: expected at least three rows.', 1;

IF ref.fnSafeRatio(7, 2) <> 3 THROW 51004, 'fnSafeRatio: 7 / 2 should be 3.', 1;
IF ref.fnSafeRatio(7, 0) IS NOT NULL THROW 51005, 'fnSafeRatio: division by zero should be NULL.', 1;

IF ref.fnFormatCustomerLabel('ab01', N'Alpha', 'a@b.example') <> N'AB01 - Alpha (b.example)'
    THROW 51006, 'fnFormatCustomerLabel: unexpected label.', 1;
IF ref.fnFormatCustomerLabel('ab01', N'Alpha', NULL) <> N'AB01 - Alpha'
    THROW 51007, 'fnFormatCustomerLabel: NULL email should leave no suffix.', 1;

IF sales.fnOrderAgeDays('2024-01-31T23:59:00', '2024-02-01T00:01:00') <> 1
    THROW 51008, 'fnOrderAgeDays: two minutes across midnight should count as 1 day.', 1;

SELECT @n = COUNT(*) FROM sales.fnOrderLines(2);
IF @n <> 3 THROW 51009, 'fnOrderLines: order 2 has three lines.', 1;

SELECT @n = COUNT(*) FROM ref.fnSplitCodes(' AB01, cd02 ,,EF03 ', ',');
IF @n <> 3 THROW 51010, 'fnSplitCodes: expected three codes.', 1;
GO

-- Procedures ----------------------------------------------------------------

DECLARE @id int, @rc int, @n int;

EXEC sales.uspGetOrdersByCustomer @CustomerCode = 'ab01  ';

-- Upsert: a new code inserts, the same code in another case updates.
EXEC ref.uspUpsertCustomer @CustomerCode = 'QR09', @Name = N'Quarry Road', @Region = 'AMER', @CustomerId = @id OUTPUT;
IF @id IS NULL THROW 51020, 'uspUpsertCustomer: insert returned no id.', 1;
DECLARE @id2 int;
EXEC ref.uspUpsertCustomer @CustomerCode = 'qr09', @Name = N'Quarry Road Ltd', @Region = 'AMER', @CustomerId = @id2 OUTPUT;
IF @id2 <> @id THROW 51021, 'uspUpsertCustomer: a code differing only in case should update, not insert.', 1;

-- Create an order through the table-valued parameter.
DECLARE @lines sales.OrderLineInput;
INSERT INTO @lines (Sku, Quantity, DiscountPct) VALUES ('WID-001', 4, 0), ('GAD-100', 1, 50);
EXEC @rc = sales.uspCreateOrder @CustomerCode = 'AB01', @Lines = @lines, @Note = N'smoke', @OrderId = @id OUTPUT;
IF @rc <> 0 OR @id IS NULL THROW 51022, 'uspCreateOrder failed.', 1;
IF (SELECT TotalAmount FROM sales.[Order] WHERE OrderId = @id) <> 19.9999
    THROW 51023, 'trOrderLine_MaintainTotal: total should be 4 * 2.50 + 19.9999 / 2 = 19.9999 (money).', 1;
IF NOT EXISTS (SELECT 1 FROM audit.OrderAudit WHERE OrderId = @id AND Action = 'I')
    THROW 51024, 'trOrder_AuditStatus: the insert was not audited.', 1;

-- A line with a wrong-case Sku must roll the whole order back.
DECLARE @bad sales.OrderLineInput;
INSERT INTO @bad (Sku, Quantity) VALUES ('gad-100', 1);
BEGIN TRY
    EXEC sales.uspCreateOrder @CustomerCode = 'AB01', @Lines = @bad, @OrderId = @id2 OUTPUT;
    THROW 51025, 'uspCreateOrder: a lower-case Sku should not match a case-sensitive column.', 1;
END TRY
BEGIN CATCH
    IF ERROR_NUMBER() <> 50001 THROW;
END CATCH;
IF NOT EXISTS (SELECT 1 FROM audit.ErrorLog WHERE ProcName = 'sales.uspCreateOrder')
    THROW 51026, 'uspLogError: the failure was not logged.', 1;

EXEC sales.uspUpdateOrderStatus @OrderId = @id, @NewStatus = 1;
IF NOT EXISTS (SELECT 1 FROM audit.OrderAudit WHERE OrderId = @id AND Action = 'U' AND OldStatus = 0 AND NewStatus = 1)
    THROW 51027, 'trOrder_AuditStatus: the status change was not audited.', 1;

EXEC @rc = sales.uspCancelOrder @OrderId = @id, @Reason = N'smoke test';
IF @rc <> 0 THROW 51028, 'uspCancelOrder failed.', 1;
EXEC @rc = sales.uspCancelOrder @OrderId = @id;
IF @rc <> 2 THROW 51029, 'uspCancelOrder: cancelling twice should return 2.', 1;

EXEC sales.uspRecalculateDailySales @FromDate = '2024-01-01', @ToDate = '2024-03-31';
IF NOT EXISTS (SELECT 1 FROM sales.DailySales WHERE SalesDate = '2024-01-31')
    THROW 51030, 'uspRecalculateDailySales: the 23:59:59.997 order belongs to 31 January.', 1;

EXEC sales.uspApplyPriceImport @RemoveMissing = 0;
IF (SELECT UnitPrice FROM ref.Product WHERE Sku = 'WID-002') <> 8.49
    THROW 51031, 'uspApplyPriceImport: staged wid-002 (padded, lower case) should update WID-002.', 1;
IF NOT EXISTS (SELECT 1 FROM ref.Product WHERE Sku = 'GAD-300')
    THROW 51032, 'uspApplyPriceImport: GAD-300 should have been inserted.', 1;

EXEC ref.uspMergeCustomerImport;
SELECT @n = COUNT(*) FROM ref.Customer WHERE CustomerCode IN ('MN07', 'OP08');
IF @n <> 2 THROW 51033, 'uspMergeCustomerImport: MN07 and OP08 should each exist once.', 1;
IF (SELECT CreditLimit FROM ref.Customer WHERE CustomerCode = 'AB01') <> 6500
    THROW 51034, 'uspMergeCustomerImport: AB01 should take the newest staged credit limit.', 1;

EXEC sales.uspCloseStaleOrders @AsOf = '2024-12-31', @StaleAfter = 30, @DryRun = 1;
EXEC sales.uspCloseStaleOrders @AsOf = '2024-12-31', @StaleAfter = 30;
EXEC sales.uspReorderReport @PackSize = 12;

EXEC @rc = sales.uspTransferStock @FromSku = 'WID-001', @ToSku = 'WID-002', @Quantity = 5;
IF @rc <> 0 THROW 51035, 'uspTransferStock failed.', 1;
EXEC @rc = sales.uspTransferStock @FromSku = 'WID-001', @ToSku = 'wid-002', @Quantity = 5;
IF @rc <> -2 THROW 51036, 'uspTransferStock: a lower-case Sku should not be found.', 1;
EXEC @rc = sales.uspTransferStock @FromSku = 'WID-002', @ToSku = 'WID-001', @Quantity = 100000;
IF @rc <> -3 THROW 51037, 'uspTransferStock: an overdraw should return -3.', 1;

EXEC sales.uspCustomerStatement @CustomerCode = 'ab01', @Year = 2024;
EXEC sales.uspSearchOrders @CustomerCode = 'AB01', @NoteContains = N'first', @SortBy = 'TotalAmount';
EXEC sales.uspSearchOrders;

EXEC stg.uspPurgeStaging @Like = 'ProductPrice%';
IF EXISTS (SELECT 1 FROM stg.ProductPriceImport) THROW 51038, 'uspPurgeStaging: the staging table was not truncated.', 1;

EXEC sales.uspArchiveOrders @OlderThan = '2024-01-01', @BatchSize = 1;
IF NOT EXISTS (SELECT 1 FROM sales.OrderArchive) THROW 51039, 'uspArchiveOrders: nothing was archived.', 1;

-- A customer with orders is deactivated by DELETE; one without orders is removed.
DELETE FROM ref.Customer WHERE CustomerCode IN ('AB01', 'KL06');
IF NOT EXISTS (SELECT 1 FROM ref.Customer WHERE CustomerCode = 'AB01' AND IsActive = 0 AND DeletedAt IS NOT NULL)
    THROW 51040, 'trCustomer_SoftDelete: AB01 has orders and should be deactivated, not deleted.', 1;
IF EXISTS (SELECT 1 FROM ref.Customer WHERE CustomerCode = 'KL06')
    THROW 51041, 'trCustomer_SoftDelete: KL06 has no orders and should be deleted.', 1;
GO

-- expect-error: Violation of UNIQUE KEY constraint
-- The second insert violates the unique code. With XACT_ABORT off and no TRY,
-- the error is reported and the batch carries on, so the first and third load.
EXEC stg.uspLoadCustomerBatch @Code1 = 'ST10', @Name1 = N'Stone', @Code2 = 'st10', @Name2 = N'Stone again',
                              @Code3 = 'UV11', @Name3 = N'Uvula';
GO

IF (SELECT COUNT(*) FROM ref.Customer WHERE CustomerCode IN ('ST10', 'UV11')) <> 2
    THROW 51042, 'uspLoadCustomerBatch: the first and third customers should have loaded.', 1;
GO
