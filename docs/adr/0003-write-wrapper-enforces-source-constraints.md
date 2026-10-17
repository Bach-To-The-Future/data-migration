---
status: accepted
---

# The write wrapper enforces the source table's constraints

Databricks does not enforce primary key, unique or foreign key constraints, and converted code may assume nothing about the target tables beyond their names and types. Legacy code relies on enforcement, most visibly "insert, catch the duplicate-key error, update instead". We decided that session state's write wrapper checks the source table's constraints from the catalog before every write (not-null, check, primary key, unique, foreign key) and raises the matching T-SQL error, because without it these procedures run without error and produce wrong data.

The full decision is in [the resolution of the ticket that made it](https://github.com/Bach-To-The-Future/data-migration/issues/10#issuecomment-6142244199).

## Considered options

- **Relying on the target to enforce them.** Rejected: Databricks treats key constraints as informational, and the tool does not create or control the target tables.
- **Rewriting each pattern that depends on enforcement.** Kept only as an optimisation: a raising transform may turn the recognised upsert pattern into a `MERGE`. It cannot be the rule, because nothing guarantees every dependent pattern is recognised.

## Consequences

- **Every write pays for a lookup** for its key constraints. The owner accepted this knowingly, and the report states it.
- **Foreign-key actions are reproduced too**: `ON DELETE` and `ON UPDATE` with `CASCADE`, `SET NULL` and `SET DEFAULT`.
- **All writes must go through the wrapper.** A write made any other way is not checked. This is one reason converted code has a single way to write.
- **The catalog must hold every constraint**, with check expressions, column defaults and foreign-key actions.
- **Enforcement is not atomic with the write.** Two jobs writing the same table at once can both pass the check. This is the single-writer assumption defined in `CONTEXT.md`, which emulated transactions and assigned identity values also rest on.

## Amendment, 18 October 2026

The single-writer assumption this rests on is restated as "at most one writer per table at any moment, converted or not, across every project in the environment", and enforced for converted jobs by a write lease: see [ADR 0021](0021-delivered-jobs-hold-a-write-lease-on-every-table-they-write.md).
