---
status: accepted
---

# A routine with a statement not generated raises before running any of it

A procedure, trigger or batch is generated statement by statement, and a statement can be one the target cannot generate exactly: manual work. We decided that one such statement makes the whole function raise when called, before any statement runs, and that none of its generated code is left in the file. The file holds the signature, the reason for each statement that is manual, and the original comments, and the report lists every construct of the function as manual work, the ones that could have been generated included, with the manual items that stop them.

The alternative is to keep the statements that were generated and fail at the one that was not. That is shorter to read and lets a person finish the missing statement in place. It is also wrong in the one way that matters: the statements before the manual one run, and a procedure that has written to a table, opened a transaction or set a session value and then stops has done half of what the original does. Nothing about the failure says so, and the half-done state is the one the source system never had. A function that cannot run at all leaves nothing to clean up, and the reason it cannot is the first thing a person reads.

## Considered options

- **Generate what can be, and raise at the manual statement.** Rejected for the half-run above. It would be safe only for a procedure with no writes before its first manual statement, and telling those apart is the work this ticket is trying not to need.
- **Generate what can be, and raise at the start of the function, leaving the code below the raise.** Rejected: unreachable code that looks as if it works, which layer 1 flags (`_unreachable`) and a reader trusts.
- **Raise before running any of it, and leave none of the code.** Chosen.

## Consequences

- **A function is generated whole or is manual work.** The manifest's status for it is "manual work", and its entry says it raises when called and which parts are manual.
- **The report is coarser than the work.** A construct that could be generated is reported as manual work because the function raises, with the reason "its code is not generated: the function raises when called", and it waits for what the manual statements of the function wait for. Each manual statement has its own item and its own reason, so what stops a function is never hidden behind the rest.
- **Coverage grows by statement kind.** A function starts to run when its last manual statement kind is generated, so most real procedures stay manual until the writes, `EXEC`, transactions, errors and cursors are generated. The statements that stop the most procedures of the corpus come first.
- **This can be revisited.** If a function is mostly generated and its manual statements are all reads or messages, running the rest would be harmless, and a later decision could allow it for statements that cannot leave the state half written. It would need that to be proved for each kind, not assumed.
