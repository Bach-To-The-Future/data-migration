---
status: accepted
---

# Transactions are emulated by restoring Delta table versions

T-SQL procedures rely on multi-statement transactions, and open-source Spark with Delta has none: each write commits on its own. We decided that converted code always uses one support-library helper, a `transaction()` block, whose default mechanism records each written table's version at the start and restores those tables to those versions on rollback. We chose it over Databricks' own transactions because it runs in the sandbox, which keeps stored procedures, the lead artefact type, reconcilable.

The full decision is in [the resolution of the ticket that made it](https://github.com/Bach-To-The-Future/data-migration/issues/10#issuecomment-6142244199).

## Considered options

- **Databricks' own transactions.** Real atomicity, but SQL only, Runtime 18 or later, Unity Catalog managed tables only, and they cannot run in the sandbox. Kept as a setting: the same generated code switches to it for a client on a suitable runtime, and reconciliation then needs a real workspace or is reported as not reconcilable locally.
- **Leaving transactions out** and reporting each one as manual. Rejected: most stored procedures would then need manual work.

## Consequences

- **This is not isolation.** Other readers see uncommitted changes, and a restore discards anything else written to those tables in the meantime. It is correct when the job owns its tables during the run and wrong under concurrent writers. The owner accepted this knowingly.
- **Every transaction is flagged** in the conversion report with that caveat.
- **Start versions are recorded in the run catalog**, so a job that crashed mid-transaction restores them first when it is resumed or re-run.
- **The helper fails loudly** if a version it would restore to no longer exists because of `VACUUM` retention.
- **Table variables are left out of the restore**, because in T-SQL a rollback does not undo changes to them.
