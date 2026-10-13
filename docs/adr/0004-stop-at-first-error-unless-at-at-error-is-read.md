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
