# PROTOTYPE: hand conversion of a corpus sample

**Throwaway.** This lives on the branch `prototype/hand-conversion` and is not meant for `master`. It exists to answer one ticket: [Which conversion steps need judgement? Hand-convert a corpus sample](https://github.com/Bach-To-The-Future/data-migration/issues/12).

## What it is

Nine artefacts from the sample corpus (`corpus/sample/artefacts`), converted by hand into PySpark following the construct mapping, with a log of every step and whether a fixed rule, a single LLM call, or something that iterates would be needed to do it.

| File | What it shows |
|---|---|
| `converted/` | The nine conversions, laid out as a converted project might be |
| `STEP-LOG.md` | Every step, classified, with the tally and the findings |
| `SUPPORT-SURFACE.md` | Every support-library call the conversions needed |

## What it is not

- **Nothing here has been run.** The support library it imports, `migration_support`, does not exist. The files parse as Python and that is all that was checked.
- **It is not real legacy code.** The nine artefacts were written for this project, by the same hand that converted them and classified the steps. Expect real client code to be messier and the classification to be optimistic.
- **Two procedures appear in two forms**, faithful and raised, side by side. A real converted project would contain one.

## The sample

| Artefact | Type | Why it was chosen |
|---|---|---|
| `sales.vOrderTotals` | View | Integer average, integer division, money arithmetic |
| `ref.fnFormatCustomerLabel` | Scalar function | Local variables and an `IF`; raised to a column expression |
| `sales.trOrder_AuditStatus` | Trigger | `inserted`, `deleted`, `IF UPDATE(col)` |
| `customer_dedup` | Batch | A `DELETE` through a common table expression with a window function |
| `sales.uspGetOrdersByCustomer` | Procedure | The simple case: one query, a case-insensitive filter |
| `ref.uspUpsertCustomer` | Procedure | Insert, catch duplicate key, update; `SCOPE_IDENTITY()`; an output parameter |
| `sales.uspCloseStaleOrders` | Procedure | A cursor; faithful and raised forms |
| `sales.uspRecalculateDailySales` | Procedure | Temp table, `WHILE` loop, transaction, `@@ROWCOUNT`; faithful and raised forms |
| `stg.uspLoadCustomerBatch` | Procedure | `@@ERROR` checks and a `GOTO` |

Not covered: table-valued functions, `MERGE`, `OUTPUT`, `INSTEAD OF` triggers, dynamic SQL, nested transactions with savepoints, procedures calling procedures beyond one call.
