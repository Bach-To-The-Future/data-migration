CREATE FUNCTION ref.fnSafeRatio (@Numerator int, @Denominator int)
RETURNS int
AS
BEGIN
    -- Integer division on purpose: 7 / 2 is 3 here, and callers rely on it.
    RETURN CASE WHEN @Denominator = 0 THEN NULL ELSE @Numerator / @Denominator END;
END;
