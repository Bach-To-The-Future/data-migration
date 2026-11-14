---
status: accepted
---

# A failed statement stops the procedure unless it reads @@ERROR

With `XACT_ABORT` off and no `TRY`, which is T-SQL's default, a statement that fails is reported and the procedure carries on with the next one. Python stops at the first exception. The tool's rule is to reproduce T-SQL exactly wherever that is possible, and here it is possible, by putting every statement in its own guarded block. We decided to do that only for procedures that read `@@ERROR`, where carrying on is clearly intended; every other procedure stops at the first error. This is a deliberate exception to the exactness rule, made because guarding every statement of every legacy procedure would make most output much harder to read, for behaviour most authors never meant.

The full decision is in [the resolution of the ticket that made it](https://github.com/Bach-To-The-Future/data-migration/issues/10#issuecomment-6142244199).

## Considered options

- **Guard every statement in every such procedure.** Exact, and consistent with the rule, but it wraps every statement of every procedure that has neither `TRY` nor `XACT_ABORT ON`.
- **Stop at the first error everywhere.** Rejected: it breaks procedures written around `@@ERROR` checks, which only work because execution continues.

## Consequences

- **The difference is reported** for every procedure that has neither `TRY` nor `XACT_ABORT ON` and does not read `@@ERROR`: if a statement fails, the original would have continued and the conversion stops.
- **Reconciliation is weaker on error paths.** For an input that makes such a procedure fail, only "an error was raised" can be compared, not the state afterwards, because the original went on to do more.
- **The guarded form depends on the error-mapping table**, which records for each error whether SQL Server terminates the statement or aborts the batch, so a guarded block continues or stops as the original would.
- **This can be revisited.** If hand conversion shows that unintended continuation matters in real client code, the alternative is to guard every statement, at the cost above.

## Amendment, 13 November 2026: three endings, not two

Running the errors the corpus raises on SQL Server ([issue #46](https://github.com/Bach-To-The-Future/data-migration/issues/46)) showed that an error ends one of three things, not two. It ends the **statement**, and the next statement of the procedure runs (constraint violations, divide by zero, `RAISERROR`). It ends the **scope**: the rest of the procedure, function or trigger does not run, and its caller goes on with the next statement (a table or column that does not exist when the statement runs). Or it ends the **batch**, and nothing after it runs (`THROW`, a failed conversion of text to a number or a date, a table that already exists). At the top level a scope-ending error is a batch-ending one. The error-mapping table records which of the three each error is, and a guarded statement continues, ends its procedure or ends everything accordingly. With `XACT_ABORT` on, or inside a `TRY` at the same call depth, a guarded statement lets the error through. The decision itself, to guard only the procedures that read `@@ERROR`, is unchanged.

## Amendment, 14 November 2026: a scope-ending error inside a transaction leaves it open

With `XACT_ABORT` off, an error that ends the scope of a procedure (a table or column that does not exist when the statement runs, 207 and 208) does not end the transaction the procedure opened: the caller goes on inside it, `@@TRANCOUNT` is unchanged, and the caller may commit it or roll it back. The support library's `transaction()` block used to roll back on any exception, which undid work SQL Server does not undo; it now lets the error that ends only the scope pass without a rollback. With `XACT_ABORT` on the error ends the batch, and the open transaction is rolled back. A procedure called with `EXEC` that ends with a different number of open transactions than it began with makes SQL Server raise error 266 in the caller, which goes on, so `@@ERROR` after such a call is 266 and not the number of the error that ended the procedure; the library raises it too, as the error class `transaction_count_mismatch`. Decided by the owner on [the errors ticket](https://github.com/Bach-To-The-Future/data-migration/issues/46), built by [issue #85](https://github.com/Bach-To-The-Future/data-migration/issues/85) and tested against SQL Server with the same procedures in both forms a transaction can take.
