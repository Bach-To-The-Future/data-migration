# PROTOTYPE: hand conversion of ref.uspUpsertCustomer. Never run; migration_support does not exist.
from pyspark.sql import functions as F

from migration_support import ProcedureResult, Session, converted, tsql

from converted.RetailSample.audit.procedures.uspLogError import uspLogError   # not converted in this prototype


@converted(artefact="ref.uspUpsertCustomer", kind="procedure")
def uspUpsertCustomer(session: Session, *, CustomerCode, Name, Region, Email=None, CustomerId=None) -> ProcedureResult:
    # @CustomerId is an OUTPUT parameter. In T-SQL it can also carry a value in, so it stays a parameter here.
    p_CustomerCode = session.param(CustomerCode, "varchar(10)")
    p_Name = session.param(Name, "nvarchar(100)")
    p_Email = session.param(Email, "varchar(100)")
    p_Region = session.param(Region, "char(5)")

    # src: BEGIN TRY INSERT INTO ref.Customer (CustomerCode, Name, Email, Region) VALUES (...);
    #                SET @CustomerId = SCOPE_IDENTITY(); END TRY
    try:
        # The wrapper enforces UQ_Customer_Code from the catalog and raises T-SQL error 2627 on a duplicate.
        # CustomerCode is case-insensitive, so 'qr09' duplicates 'QR09'.
        session.insert_values("ref.Customer", CustomerCode=p_CustomerCode, Name=p_Name, Email=p_Email, Region=p_Region)
        CustomerId = session.scope_identity()

    # src: BEGIN CATCH ... END CATCH
    except Exception as exc:
        err = session.catch(exc)   # re-raises what T-SQL's CATCH would not have caught

        # src: IF ERROR_NUMBER() NOT IN (2601, 2627) BEGIN EXEC audit.uspLogError @ProcName = '...'; THROW; END
        if err.number not in (2601, 2627):
            uspLogError(session, ProcName="ref.uspUpsertCustomer")
            raise

        # src: UPDATE ref.Customer SET Name = @Name, Email = ISNULL(@Email, Email), Region = @Region
        #      WHERE CustomerCode = @CustomerCode;
        same_code = tsql.ci_eq(F.col("CustomerCode"), p_CustomerCode)
        session.update(
            "ref.Customer",
            set={"Name": p_Name, "Email": tsql.isnull(p_Email, F.col("Email"), "varchar(100)"), "Region": p_Region},
            where=same_code,
        )

        # src: SELECT @CustomerId = CustomerId FROM ref.Customer WHERE CustomerCode = @CustomerCode;
        # No row leaves the variable as it was. The unique constraint means at most one row matches.
        CustomerId = session.assign_from_query(
            CustomerId, session.table("ref.Customer").where(same_code).select("CustomerId")
        )

    return ProcedureResult(return_code=0, outputs={"CustomerId": CustomerId}, result_sets=[])
