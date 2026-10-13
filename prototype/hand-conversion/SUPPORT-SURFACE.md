# Support-library surface, as the hand conversions used it

None of this exists. The conversions were written as if it did, and this is the list of what they called. It is a first view of the interface, taken from use, not a design.

Nine artefacts needed 24 members of session state, 15 column helpers and 2 scalar helpers.

## Decorator

| Call | Purpose |
|---|---|
| `@converted(artefact=, kind=, …)` | Marks a converted function with the artefact it came from; the natural place to pin ANSI mode on entry and to record form and transforms |

## Session state

**Reading**

| Call | Purpose | Used by |
|---|---|---|
| `session.table(name)` | A table, temp table or view by its source name | 6 |
| `session.param(value, type)` | A parameter as a typed literal column | 5 |
| `session.literal(value, type)` | A typed literal column | 1 |
| `session.value(value, type)` | A parameter or variable as a typed Python value | 1 |
| `session.scalar(df)` | The single value of a one-row, one-column query | 1 |
| `session.assign_from_query(current, df)` | `SELECT @x = col`: keeps the old value when no row comes back | 1 |
| `session.cursor(df)` | Rows streamed one at a time from a snapshot | 1 |
| `session.snapshot(df)` | Fixes a DataFrame's rows before something it reads is written | 1 |
| `session.row(Name=(value, type), …)` | A one-row result set built from variables | 3 |

**Writing**

| Call | Purpose | Used by |
|---|---|---|
| `session.insert(table, df)` | `INSERT … SELECT` | 2 |
| `session.insert_values(table, **columns)` | `INSERT … VALUES` | 2 |
| `session.update(table, set=, where=)` | `UPDATE … WHERE` | 2 |
| `session.update_from(table, source=, on=, set=)` | Update the rows matching a set of keys | 1 |
| `session.delete(table, where=)` | `DELETE … WHERE` | 1 |
| `session.replace_rows(table, df, as_statement=)` | Replace a table's contents, counted as the statement it stands for | 1 |
| `session.create_temp_table(name, columns=, primary_key=)` | A temp table | 1 |
| `session.drop_temp_table(name)` | Drop it | 1 |

**State and control**

| Call | Purpose | Used by |
|---|---|---|
| `session.rowcount` | `@@ROWCOUNT` | 1 |
| `session.scope_identity()` | `SCOPE_IDENTITY()` | 1 |
| `session.at_error` | `@@ERROR` | 1 |
| `session.transaction()` and `tx.rollback()` | A transaction block, with an explicit rollback | 2 |
| `session.statement()` | A guarded statement: a failure is recorded and execution continues | 1 |
| `session.catch(exc)` | In an `except`: the error as T-SQL would report it; re-raises what `CATCH` would not have caught | 1 |
| `session.print(text)` | `PRINT` | 1 |

## Column helpers

| Call | Purpose |
|---|---|
| `tsql.ci_eq(a, b)` | Equality ignoring case and trailing spaces |
| `tsql.ci_key(col)` | The normalised value to group, partition or count distinct on |
| `tsql.nullif_ci(a, b)` | `NULLIF` under a case-insensitive collation |
| `tsql.int_div(a, b)` | Integer division truncating toward zero |
| `tsql.avg_int(col)` | `AVG` over an integer column |
| `tsql.money(expr)` | Truncate to `money`'s four places |
| `tsql.isnull(a, b, type)` | `ISNULL`, typed as its first argument |
| `tsql.as_type(expr, type)` | Assignment to a typed variable or parameter, cutting silently |
| `tsql.len(col)` | `LEN`, ignoring trailing spaces |
| `tsql.charindex(needle, haystack)` | `CHARINDEX` |
| `tsql.substring(col, start, length)` | `SUBSTRING` |
| `tsql.stuff(col, start, length, new)` | `STUFF` |
| `tsql.datediff(part, start, end)` | `DATEDIFF`, counting boundaries |
| `tsql.datetime(text)` | A date-time literal from a T-SQL string |

## Scalar helpers, on Python values

| Call | Purpose |
|---|---|
| `tsql.scalar.le(a, b)` | `<=` where either side may be `NULL` |
| `tsql.scalar.dateadd(part, n, value)` | `DATEADD` on a single value |

## What stands out

- **Most of session state is small wrappers**, each standing for one T-SQL statement or one piece of session state. Nine artefacts found 24; the full corpus will find more, but the shape is clear.
- **Two calls exist only because DataFrames are lazy**: `session.snapshot` and the snapshot inside `session.cursor`.
- **`session.param`, `session.literal` and `session.value` are three ways to bring a value in**, as a column or as a Python value, each with a type. They could probably be one.
- **The scalar helpers are the thin end of a wedge.** Only two were needed here. Any procedure with arithmetic or string handling on variables will need more, each a pure-Python twin of a column helper.
