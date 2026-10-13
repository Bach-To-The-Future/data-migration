# PROTOTYPE: hand conversion of stg.uspLoadCustomerBatch. Never run; migration_support does not exist.
from migration_support import ProcedureResult, Session, converted


@converted(artefact="stg.uspLoadCustomerBatch", kind="procedure", form="raised",
           transforms=("goto_exit_label_to_branch",), guarded_statements=True)
def uspLoadCustomerBatch(session: Session, *, Code1, Name1, Code2, Name2, Code3, Name3) -> ProcedureResult:
    """This procedure reads @@ERROR, so every statement is guarded (ADR 0004): a statement that fails is
    recorded and the procedure carries on, as T-SQL does with XACT_ABORT off and no TRY.

    The GOTO to the exit label at the end became the body of an if. The transaction has two ends
    (COMMIT on one path, ROLLBACK on the other), so the block is given an explicit rollback.
    """
    codes = [session.param(c, "varchar(10)") for c in (Code1, Code2, Code3)]
    names = [session.param(n, "nvarchar(100)") for n in (Name1, Name2, Name3)]

    # src: SELECT @loaded = 0, @failed = 0;
    loaded = 0
    failed = 0

    # src: BEGIN TRANSACTION;
    with session.transaction() as tx:

        # src (three times): INSERT INTO ref.Customer (CustomerCode, Name, Region) VALUES (@CodeN, @NameN, 'EMEA');
        #                    SELECT @err = @@ERROR;
        #                    IF @err = 0 SET @loaded = @loaded + 1 ELSE SET @failed = @failed + 1;
        # The original repeats the three statements; they are written out here too, not folded into a loop,
        # so that each stays beside its source lines.
        with session.statement():
            session.insert_values("ref.Customer", CustomerCode=codes[0], Name=names[0], Region=session.literal("EMEA", "char(5)"))
        err = session.at_error
        if err == 0:
            loaded = loaded + 1
        else:
            failed = failed + 1

        with session.statement():
            session.insert_values("ref.Customer", CustomerCode=codes[1], Name=names[1], Region=session.literal("EMEA", "char(5)"))
        err = session.at_error
        if err == 0:
            loaded = loaded + 1
        else:
            failed = failed + 1

        with session.statement():
            session.insert_values("ref.Customer", CustomerCode=codes[2], Name=names[2], Region=session.literal("EMEA", "char(5)"))
        err = session.at_error
        if err == 0:
            loaded = loaded + 1
        else:
            failed = failed + 1

        # src: IF @loaded = 0 GOTO nothing_loaded;
        #      nothing_loaded: ROLLBACK TRANSACTION; SELECT 0 AS Loaded, @failed AS Failed; RETURN 1;
        if loaded == 0:
            tx.rollback()
            return ProcedureResult(
                return_code=1,
                result_sets=[session.row(Loaded=(0, "int"), Failed=(failed, "int"))],
            )

    # src: COMMIT TRANSACTION; SELECT @loaded AS Loaded, @failed AS Failed; RETURN 0;
    return ProcedureResult(
        return_code=0,
        result_sets=[session.row(Loaded=(loaded, "int"), Failed=(failed, "int"))],
    )
