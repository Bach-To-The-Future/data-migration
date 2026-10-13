CREATE TRIGGER sales.trOrder_AuditStatus
ON sales.[Order]
AFTER INSERT, UPDATE
AS
BEGIN
    SET NOCOUNT ON;

    -- Fires once per statement, not once per row: inserted and deleted can
    -- each hold many rows.
    IF NOT EXISTS (SELECT 1 FROM inserted)
        RETURN;

    IF NOT EXISTS (SELECT 1 FROM deleted)
    BEGIN
        INSERT INTO audit.OrderAudit (OrderId, Action, OldStatus, NewStatus)
        SELECT i.OrderId, 'I', NULL, i.Status
        FROM inserted AS i;

        RETURN;
    END;

    -- UPDATE(Status) is true when the column appears in the SET list, even if
    -- the value did not change, so the comparison below is still needed.
    IF UPDATE(Status)
        INSERT INTO audit.OrderAudit (OrderId, Action, OldStatus, NewStatus)
        SELECT i.OrderId, 'U', d.Status, i.Status
        FROM inserted AS i
        JOIN deleted AS d
            ON d.OrderId = i.OrderId
        WHERE d.Status <> i.Status;
END;
