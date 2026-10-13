# PROTOTYPE: hand conversion of ref.fnFormatCustomerLabel. Never run; migration_support does not exist.
from pyspark.sql import Column, functions as F

from migration_support import Session, converted, tsql


@converted(artefact="ref.fnFormatCustomerLabel", kind="scalar_function", returns="nvarchar(200)")
def fnFormatCustomerLabel(session: Session, CustomerCode: Column, Name: Column, Email: Column) -> Column:
    """Raised to a column expression: the body has local variables and one IF, no loop and no data access.

    Each variable assignment became a new expression; the IF became when/otherwise.
    """
    # Parameters are typed: a longer value is cut to the declared length without an error.
    CustomerCode = tsql.as_type(CustomerCode, "varchar(10)")
    Name = tsql.as_type(Name, "nvarchar(100)")
    Email = tsql.as_type(Email, "varchar(100)")

    # src: DECLARE @at int = CHARINDEX('@', @Email);
    at = tsql.charindex(F.lit("@"), Email)

    # src: SET @domain = CASE WHEN @at > 0 THEN SUBSTRING(@Email, @at + 1, LEN(@Email)) ELSE '' END;
    domain = tsql.as_type(
        F.when(at > 0, tsql.substring(Email, at + 1, tsql.len(Email))).otherwise(F.lit("")),
        "varchar(100)",
    )

    # src: SET @label = UPPER(RTRIM(@CustomerCode)) + N' - ' + @Name + ISNULL(N' (' + NULLIF(@domain, '') + N')', N'');
    # '+' gives NULL when any part is NULL, which is what Spark's concat does.
    # NULLIF compares a variable with a literal, so it follows the database's default collation:
    # case-insensitive, and '' equals any run of spaces.
    label = tsql.as_type(
        F.concat(
            F.upper(F.rtrim(CustomerCode)),
            F.lit(" - "),
            Name,
            F.coalesce(F.concat(F.lit(" ("), tsql.nullif_ci(domain, F.lit("")), F.lit(")")), F.lit("")),
        ),
        "nvarchar(200)",
    )

    # src: IF LEN(@CustomerCode) > 6 SET @label = ISNULL(STUFF(@label, 3, 2, '**'), @label);
    label = F.when(
        tsql.len(CustomerCode) > 6,
        tsql.as_type(F.coalesce(tsql.stuff(label, 3, 2, F.lit("**")), label), "nvarchar(200)"),
    ).otherwise(label)

    # src: RETURN @label;
    return label
