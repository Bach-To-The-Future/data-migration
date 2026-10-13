# Step log

For each step: what was done, which stage it belongs to, and what could have done it.

- **Rule**: a fixed rule could do it, given the catalog.
- **LLM step**: one model call with a fixed prompt could do it; no fixed rule is obvious.
- **Iterate**: needs something that tries, inspects a result and tries again.

Stages are lowering (L), raising (R) and generation (G).

## Tally

| | Steps | Rule | LLM step | of which need Iterate |
|---|---|---|---|---|
| Faithful conversion of all nine | 69 | 68 | 1 | 0 |
| Raising (two procedures) | 8 | 2 | 6 | 2 |
| **Total** | **77** | **70** | **7** | **2** |

- Every step of faithful conversion was rule-shaped except one unusual statement: a `DELETE` whose target is a common table expression with a window function.
- Every other step needing judgement was in raising.
- "Rule" hides one large thing: nearly every expression needed its T-SQL type worked out from the catalog (step 2 of the view). That is deterministic, and it is the heaviest single piece of lowering.

**How much to trust this.** The classification is one person's judgement of their own work, on artefacts they also wrote, and none of the code was run. Three mistakes were made during conversion and caught only by re-reading; they are listed at the end. There may be others.

## sales.vOrderTotals (view)

| # | Step | Stage | Class |
|---|---|---|---|
| 1 | `FROM`, `LEFT JOIN`, `GROUP BY` into relational steps | L | Rule |
| 2 | Type of every column from the catalog, and of every expression by T-SQL's precedence rules | L | Rule |
| 3 | `AVG` over an `int` column is a truncating integer average of the non-null values | L | Rule |
| 4 | `SUM(..) / NULLIF(COUNT(..), 0)` is integer division | L | Rule |
| 5 | `int * money * int / int` is `money` at each step, truncated to four places | L | Rule |
| 6 | `COUNT` and `SUM` return `int`, and overflow is an error | L | Rule |
| 7 | No grouping column is a case-insensitive string | L | Rule |
| 8 | Emit join, group, aggregate, select in the source's order | G | Rule |

## ref.fnFormatCustomerLabel (scalar function)

| # | Step | Stage | Class |
|---|---|---|---|
| 1 | Declarations, assignments, one `IF` | L | Rule |
| 2 | Typed parameters and variables cut a longer value silently | L | Rule |
| 3 | The body has no loop and no data access, so it can be a column expression | R | Rule |
| 4 | Each assignment becomes a new expression; the `IF` becomes `when`/`otherwise` | R | Rule |
| 5 | `CHARINDEX`, `SUBSTRING`, `LEN`, `STUFF` to helpers | G | Rule |
| 6 | `+` on strings to `concat`, which also gives `NULL` if any part is `NULL` | G | Rule |
| 7 | `NULLIF(@domain, '')` compares under the database's default collation | L | Rule |
| 8 | Emit | G | Rule |

## sales.trOrder_AuditStatus (trigger)

| # | Step | Stage | Class |
|---|---|---|---|
| 1 | Table, timing, events and body | L | Rule |
| 2 | `inserted` and `deleted` become DataFrame arguments | G | Rule |
| 3 | `IF NOT EXISTS (SELECT 1 FROM inserted)` to an emptiness check | G | Rule |
| 4 | `IF UPDATE(Status)` to membership in the set of columns the `UPDATE` named | G | Rule |
| 5 | An `INSERT` naming some columns leaves identity and defaults to the write wrapper | G | Rule |
| 6 | Emit | G | Rule |

## customer_dedup (batch)

| # | Step | Stage | Class |
|---|---|---|---|
| 1 | Split into batches; common table expression, `DELETE`, `SELECT` | L | Rule |
| 2 | `PARTITION BY` a case-insensitive column uses its normalised value | L | Rule |
| 3 | A `DELETE` through a common table expression, conditioned on a window function, on a table with no key: turned inside out into "replace the contents with the rows kept" | G | **LLM step** |
| 4 | The window's ordering is not total, so which duplicate survives is undefined: caveat | L | Rule |
| 5 | `COUNT(DISTINCT ..)` on that column uses its normalised value | L | Rule |
| 6 | Emit | G | Rule |

## sales.uspGetOrdersByCustomer (procedure)

| # | Step | Stage | Class |
|---|---|---|---|
| 1 | One query | L | Rule |
| 2 | Typed parameters | L | Rule |
| 3 | `c.CustomerCode = @CustomerCode` is case-insensitive | L | Rule |
| 4 | `ISNULL(@FromDate, '19000101')` converts the string to a date-time | L | Rule |
| 5 | A scalar function call becomes a call to its column-expression form | G | Rule |
| 6 | The callee uses `GETDATE()`, so it needs session state | G | Rule |
| 7 | Result set, with its `ORDER BY`, into the result object | G | Rule |

## ref.uspUpsertCustomer (procedure)

| # | Step | Stage | Class |
|---|---|---|---|
| 1 | `TRY`/`CATCH`, `INSERT … VALUES`, `UPDATE`, variable assignment from a query | L | Rule |
| 2 | An `OUTPUT` parameter is also an input | G | Rule |
| 3 | `TRY`/`CATCH` to `try`/`except` through the catch helper | G | Rule |
| 4 | `ERROR_NUMBER() NOT IN (2601, 2627)`: both numbers are in the error mapping | G | Rule |
| 5 | `EXEC` to a function call; `THROW` to `raise` | G | Rule |
| 6 | `UPDATE … WHERE` on a case-insensitive column; `ISNULL` keeps the first argument's type | L | Rule |
| 7 | `SELECT @CustomerId = …`: the unique constraint shows at most one row, so no caveat | L | Rule |

The optional raising of this pattern to a `MERGE` was not applied.

## sales.uspCloseStaleOrders (procedure with a cursor)

**Faithful form**

| # | Step | Stage | Class |
|---|---|---|---|
| 1 | Cursor declaration, open, fetch, loop, close, deallocate | L | Rule |
| 2 | Fetch-before-loop plus fetch-at-end plus `WHILE @@FETCH_STATUS = 0` is "for each row" | G | Rule |
| 3 | `DATEDIFF` to its helper; `IN` | G | Rule |
| 4 | `SELECT @Lines = COUNT(*)` always returns one row, so a plain scalar | L | Rule |
| 5 | `IF` / `ELSE IF` / `ELSE`; `@DryRun = 0` behaves the same in Python when `@DryRun` is `NULL` | G | Rule |
| 6 | `UPDATE` by key | G | Rule |
| 7 | `PRINT` to the session's message log | G | Rule |
| 8 | The loop writes the table the cursor reads: caveat | L | Rule |
| 9 | Emit | G | Rule |

**Raised form**

| # | Step | Stage | Class |
|---|---|---|---|
| 10 | Establish that the passes are independent: each reads only its own row and a table no pass writes, and writes only its own order | R | **LLM step, Iterate** |
| 11 | The per-row count becomes one grouped count, joined in | R | **LLM step** |
| 12 | The three branches become three disjoint sets; the counters become their sizes | R | **LLM step** |
| 13 | The sets must be fixed before the updates, because the updates change what the query reads | G | Rule |
| 14 | `PRINT` runs for the leftover rows in the cursor's order | R | **LLM step** |

## sales.uspRecalculateDailySales (procedure with a temp table and a loop)

**Faithful form**

| # | Step | Stage | Class |
|---|---|---|---|
| 1 | Declarations, temp table, loop, transaction | L | Rule |
| 2 | `#Sales` with its primary key to a session temp table | G | Rule |
| 3 | `WHILE @Day <= @ToDate`: either side may be `NULL`, so the comparison needs T-SQL's rules, not Python's | G | Rule |
| 4 | `INSERT … SELECT` with the loop variable as a literal | G | Rule |
| 5 | `CAST(o.OrderDate AS date)` | G | Rule |
| 6 | Money aggregate | L | Rule |
| 7 | `DATEADD` on a scalar needs a scalar helper | G | Rule |
| 8 | `BEGIN TRANSACTION … COMMIT` in a straight line to a transaction block | R | Rule |
| 9 | `@@ROWCOUNT` read straight after a write | G | Rule |
| 10 | `DROP TABLE #Sales`; the result row | G | Rule |
| 11 | No `TRY`, `XACT_ABORT` off, inside a transaction: caveat under ADR 0004 | L | Rule |

**Raised form**

| # | Step | Stage | Class |
|---|---|---|---|
| 12 | The loop steps one day at a time and uses its variable only as a filter and an output value, so it is a `GROUP BY` over the range | R | **LLM step, Iterate** |
| 13 | `#Sales` is filled once, then read, and its sources are not written first: a DataFrame | R | Rule |
| 14 | With a `NULL` bound the loop runs zero times and `BETWEEN` selects nothing: the same | R | **LLM step** |

## stg.uspLoadCustomerBatch (procedure with @@ERROR and GOTO)

| # | Step | Stage | Class |
|---|---|---|---|
| 1 | Statements, label, `GOTO` | L | Rule |
| 2 | The procedure reads `@@ERROR`, so every statement is guarded | G | Rule |
| 3 | `SELECT @err = @@ERROR` to the session's last error | G | Rule |
| 4 | A forward `GOTO` to a label that ends the procedure becomes the body of an `if` | R | Rule |
| 5 | A transaction with a `COMMIT` on one path and a `ROLLBACK` on the other: a block with an explicit rollback | R | Rule |
| 6 | The literal `'EMEA'` going into a `char(5)` column | L | Rule |
| 7 | The three repeated statement groups stay written out, each beside its source | G | Rule |

## Findings

Things the conversion ran into that the construct mapping did not settle.

1. **Procedural code is not "plain Python".** The construct mapping classes variables, `IF` and `WHILE` as plain Python. Three things got in the way:
   - a comparison with `NULL` is "unknown" in T-SQL and an exception in Python (`@Day <= @ToDate`);
   - typed variables and parameters cut or convert values on assignment;
   - scalar functions such as `DATEADD` need T-SQL's behaviour on single values, outside any DataFrame.

   The conversions use plain Python where a value provably cannot be `NULL`, and a scalar helper otherwise. That means pure-Python twins of helpers are needed for procedural code, not only inside Python UDFs.
2. **Collation applies to variables and literals too.** `NULLIF(@domain, '')` compares under the database's default collation. The mapping speaks only of columns.
3. **`IF UPDATE(col)` needs the write wrapper to pass on which columns the `UPDATE` named.** It cannot be worked out from the inserted and deleted rows.
4. **Constraint enforcement has to follow collation.** Under a case-insensitive unique constraint, `'qr09'` duplicates `'QR09'`.
5. **Lazy evaluation is a trap.** A DataFrame is recomputed when used. In the raised cursor form, the sets of orders had to be fixed before the updates, or the second update would have read the first one's changes. The rule is the same one the temp-table decision already uses: anything read after a write to one of its sources must be materialised first.
6. **Every converted function needs session state**, including scalar functions, because of `GETDATE()`.
7. **Money arithmetic is noisy.** Every operation is wrapped to truncate to four places, which makes simple expressions hard to read.
8. **`PRINT` output.** It is not part of an observation as defined, so nothing would check its content or order.
9. **A transaction block and ADR 0004 interact.** Where the original would carry on after a failed statement and commit partial work, the conversion raises and the block rolls everything back.
10. **A cursor's sensitivity is not always defined.** `FAST_FORWARD` does not promise whether the cursor sees changes made during the loop. The conversion always uses a snapshot.

## Mistakes made while converting

All three were caught by re-reading, none by running anything.

1. `AVG(ol.Quantity)` was first written as the sum divided by the count of lines. `AVG` counts non-null values of its own argument.
2. In the raised cursor form the three sets were first left lazy, so they would have been recomputed after the first update.
3. A set of keys was first collected to the driver for an `IN` list, which does not scale; it became a DataFrame passed to an update-from.

Mistakes 1 and 2 would have produced wrong results without any error. Both are the kind a rule gets right every time.
