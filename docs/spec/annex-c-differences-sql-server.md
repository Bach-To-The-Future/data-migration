# Annex C. Silent differences: the SQL Server source half

Part of the [v1 architecture spec](v1-architecture-spec.md). This half says how each SQL Server construct behaves, which lowering resolves into an operation with an explicit meaning, and which the test-data generator's edge-case pools exercise.

Entries are copied unchanged from [the research catalogue](../research/tsql-spark-silent-differences.md), the output of [Where do T-SQL and Spark silently disagree?](https://github.com/Bach-To-The-Future/data-migration/issues/6), and keep its IDs, so an entry's two halves can be read side by side. The split follows [ADR 0019](../adr/0019-shared-mappings-are-split-by-source-and-by-target.md). Sections 10 (behaviours that agree) and 11 (what could not be established) stay in the research catalogue, with its method and evidence labels.

Fourteen entries were marked "Inferred" from documentation; thirteen were later confirmed on SQL Server and local Spark, and entry 8.5 was refuted (no difference). Those results are in [`corpus/differences/results.md`](../../corpus/differences/results.md), not in the entries below.

The **Vocabulary** column names the operations and types in [Annex A](../spec-drafts/operation-vocabulary.md) that carry each entry's meaning. Lowering resolves the construct into them, using catalog types and collations; where meaning depends on operand types, it chooses the operation that means exactly that. A comparison rule is the case, accent and trailing-space behaviour taken from SQL Server's collation precedence.

## 1. ANSI mode

| ID | Example | Vocabulary | T-SQL | Evidence |
|---|---|---|---|---|
| 1.1 | `SELECT 2147483647 + 1` | `add` on `integer` (overflow is an error) | Arithmetic overflow error; the query ends ([ARITHABORT][m-arithabort]) | Both |
| 1.2 | `SELECT 1 / 0` | `integer_divide`, `decimal_divide` (division by zero is an error) | Divide-by-zero error. `NULL` only if `ANSI_WARNINGS` and `ARITHABORT` are both `OFF` ([ARITHABORT][m-arithabort]) | Both |
| 1.3 | `CAST('a' AS int)` | `convert` (failure is an error; `try_convert` gives null) | Error ([CAST and CONVERT][m-cast]) | Both |
| 1.4 | `CAST(12345 AS tinyint)` | `convert` to `integer` (8, unsigned) | Overflow error ([ARITHABORT][m-arithabort]) | Both |
| 1.5 | `10 = '10.1'` (integer against string) | `implicit_convert`, then `compare` | The string is converted to `int` because `int` has the higher precedence, and a string that is not an integer fails ([data type precedence][m-precedence], [data type conversion][m-conversion]) | Both |
| 1.6 | `'5' + '7.6'` (two strings) | `concat_null_propagating`, chosen by operand types | `'57.6'`: `+` concatenates strings ([string concatenation][m-stringconcat]) | Both |
| 1.7 | `SELECT 5 UNION ALL SELECT '6'` (also `CASE` and `COALESCE` with mixed branches) | `implicit_convert` in `union`, `case`, `first_non_null` | `int` column: the lower-precedence string is converted ([data type precedence][m-precedence], [COALESCE][m-coalesce]) | Both |
| 1.8 | A statement in `BEGIN TRY` that overflows, divides by zero or fails a cast | `try_catch` | Control passes to `CATCH` ([TRY...CATCH][m-trycatch]) | Both |

## 2. Collation, case sensitivity and trailing spaces

| ID | Example | Vocabulary | T-SQL (default collation) | Evidence |
|---|---|---|---|---|
| 2.1 | `'abc' = 'ABC'` | `compare` with the column's comparison rule | True under a `_CI` collation ([collation][m-collation]) | Both |
| 2.2 | The same values as keys in `JOIN`, `GROUP BY`, `DISTINCT`, `IN`, set operators and `MERGE … ON` | `join`, `aggregate`, `distinct`, `in_list`, `union`, `merge_rows`, each with a comparison rule | Values differing only in case match or fall in one group. Neither page names these operators; this follows from the comparison rule | Inferred |
| 2.3 | `'Cafe' = 'Café'` | `compare` (accent rule) | False under `_AS`, true under `_AI` ([collation][m-collation]) | Both |
| 2.4 | `'abc' = 'abc '` | `compare` (trailing-space rule) | True: strings are padded to equal length before comparison, whatever `ANSI_PADDING` is ([string comparison][m-strcmp]) | Both |
| 2.5 | `'' = '  '`, so `WHERE col = ''` | `compare` (trailing-space rule) | True, by the padding rule in 2.4 ([string comparison][m-strcmp]) | Both |
| 2.6 | `'abc ' LIKE 'abc'` on `varchar` | `like` with its rule | True: trailing blanks in the matched expression are ignored for non-Unicode data, but are significant for `nvarchar` ([LIKE][m-like]) | Both |
| 2.7 | `ORDER BY`, `MIN`, `MAX`, `<`, `BETWEEN` on strings such as `'apple'` and `'Banana'` | `sort`, `minimum`, `maximum`, `compare`, `between`, each with a comparison rule | Collation order; with a `_CI` collation `'apple'` sorts before `'Banana'` (derived) ([collation][m-collation]) | Both |
| 2.8 | `'ABC' LIKE 'a%'` | `like` with its rule | True under a `_CI` collation ([LIKE][m-like], [collation][m-collation]) | Both |
| 2.9 | `'apple' LIKE '[a-c]%'` | `like` with its rule (character classes) | True: `[ ]` and `[^ ]` are wildcards ([LIKE][m-like]) | Both |
| 2.10 | `col LIKE 'a\_b'` | `like` with its rule (escape character) | No default escape character, so `\` is literal and `_` matches any character ([LIKE][m-like]) | Both |
| 2.11 | `REPLACE('ABCabc', 'abc', 'x')`; also `CHARINDEX` | `replace_text`, `position_of`, each with a comparison rule | `'xx'` under a `_CI` collation (derived): both functions compare using the input collation ([REPLACE][m-replace], [CHARINDEX][m-charindex]) | Both |
| 2.12 | `CAST('ab' AS char(5)) + '!'` | `string` (fixed), `convert` | `'ab   !'` (derived): `char(n)` is fixed-size and padded with blanks ([char and varchar][m-charvarchar], [ANSI_PADDING][m-ansipadding]) | Both |
| 2.13 | `CAST('abcdef' AS varchar(3))`; `CAST(x AS varchar)` (length 30); `DECLARE @v varchar` (length 1) | `fit_to_type`, `convert` | Silently truncated to the length ([char and varchar][m-charvarchar], [CAST and CONVERT][m-cast]) | Both |

## 3. Numbers: division, implicit conversion, decimal precision

| ID | Example | Vocabulary | T-SQL | Evidence |
|---|---|---|---|---|
| 3.1 | `5 / 2` on integers | `integer_divide` | `2`: the fractional part is truncated ([division][m-divide]) | Both |
| 3.2 | `AVG(c)` over an `int` column holding 1 and 2 | `average` (result type) | `1` (derived): the return type is `int` ([AVG][m-avg]) | Both |
| 3.3 | `AVG(c)` over `decimal(p, s)` | `average` (result type) | `decimal(38, max(s, 6))` ([AVG][m-avg]) | Both |
| 3.4 | `SUM(c)` | `sum` (result type) | `int` for `int` input, so it fails above the `int` range; `decimal(38, s)` for decimal input ([SUM][m-sum]) | Both |
| 3.5 | `POWER(2, -100.0)` | `numeric_function` (`POWER`) | `0`: the return type follows the first argument, here `int` ([POWER][m-power]) | Both |
| 3.6 | `ROUND(123.456, 2, 1)` | `round` (truncate) | `123.450` (derived): a non-zero third argument truncates, and the return type keeps the input's precision and scale ([ROUND][m-round]) | Both |
| 3.7 | `tinyint` value `200` | `integer` (8, unsigned) | Valid: the range is 0 to 255 ([integer types][m-int]) | Both |
| 3.8 | `CAST(12345678901 AS decimal)` | `decimal`, `convert` | Fits: the default is `decimal(18, 0)` ([decimal and numeric][m-decimal]) | Both |
| 3.9 | `CAST('' AS int)` | `convert` | `0` ([char and varchar][m-charvarchar]) | Both |
| 3.10 | `CAST(CAST(1234567.891 AS float) AS varchar(30))` | `convert`, `convert_with_style` | At most 6 digits, in scientific notation when needed (style 0) ([CAST and CONVERT][m-cast]) | Both |
| 3.11 | `money` arithmetic, for example `money / money` | `money`, `money_multiply`, `money_divide` | `money` keeps four decimal places and Microsoft warns of rounding through truncation ([money][m-money]) | Inferred |
| 3.12 | `ISNUMERIC('$')` | `is_numeric_text` | `1`; also `1` for `+` and `-` ([ISNUMERIC][m-isnumeric]) | Inferred |
| 3.13 | `SUM(c)` or `AVG(c)` over a `float` or `real` column | `sum`, `average` on `float` | The order in which rows are added is not defined, and a parallel plan can change it from one run to the next. Floating-point addition is not associative, so the last digits can differ | Inferred. Added by the owner in [How does reconciliation generate data and compare results?](https://github.com/Bach-To-The-Future/data-migration/issues/13) |

## 4. Dates and times

| ID | Example | Vocabulary | T-SQL | Evidence |
|---|---|---|---|---|
| 4.1 | `CAST('2024-01-01 23:59:59.999' AS datetime)` | `local_timestamp` (rounding), `convert` | `2024-01-02 00:00:00.000`: `datetime` rounds to increments of .000, .003 or .007 seconds ([datetime][m-datetime]) | Both |
| 4.2 | `CAST('2024-05-09 23:59:59' AS smalldatetime)` | `local_timestamp` (rounding), `convert` | `2024-05-10 00:00:00`: rounds to the nearest minute ([smalldatetime][m-smalldatetime]) | Both |
| 4.3 | `datetime2(7)`, `time(7)`, `datetimeoffset(7)`, `SYSDATETIME()` | `local_timestamp`, `time_of_day`, `timestamp_with_offset` (digits); `current_time` | Seven fractional digits, 100 ns ([datetime2][m-datetime2], [SYSDATETIME][m-sysdatetime]) | Both |
| 4.4 | `datetimeoffset` value `2024-05-08 12:35:29 +12:15` | `timestamp_with_offset` | The offset is preserved for retrieval; comparison is in UTC ([datetimeoffset][m-datetimeoffset]) | Both |
| 4.5 | `datetime` or `datetime2` mapped to Spark `TIMESTAMP` | `local_timestamp` | No time zone ([datetime2][m-datetime2]) | Both |
| 4.6 | `GETDATE()`, `CAST(GETDATE() AS date)` | `current_time` | The local time of the server's operating system, as `datetime` ([GETDATE][m-getdate]) | Both |
| 4.7 | `DATEDIFF(year, '2005-12-31 23:59:59.9999999', '2006-01-01 00:00:00.0000000')` | `date_diff_boundaries` | `1`: counts datepart boundaries crossed ([DATEDIFF][m-datediff]) | Both |
| 4.8 | `DATEDIFF(day, @start, @end)` | `date_diff_boundaries` | Start first, end second ([DATEDIFF][m-datediff]) | Both |
| 4.9 | `DATEPART(weekday, '2007-04-21')` (a Saturday) | `date_part`, with `set_session_option` (`DATEFIRST`) | Depends on `SET DATEFIRST`: `7` under the US English default of 7, `6` under `DATEFIRST 1` ([DATEPART][m-datepart], [DATEFIRST][m-datefirst]) | Both |
| 4.10 | `DATEPART(week, '2021-01-01')` | `date_part` | `1` (derived): 1 January always starts week 1, and the week start follows `DATEFIRST` ([DATEPART][m-datepart]) | Both |
| 4.11 | `CAST(0 AS datetime)`, `DATEADD(day, DATEDIFF(day, 0, @d), 0)`, `CAST(@d AS int)` | `convert`, `date_add`, `date_diff_boundaries` | `0` is 1 January 1900 and integers count days ([DATEPART][m-datepart], [CAST and CONVERT][m-cast]) | Both |
| 4.12 | `CAST('' AS datetime)` | `convert` | `1900-01-01` ([char and varchar][m-charvarchar]) | Both |
| 4.13 | `CAST('01/02/2024' AS date)` | `convert`, `is_date_text`, with `set_session_option` (`DATEFORMAT`, language) | Depends on `SET DATEFORMAT` and `SET LANGUAGE`; such conversions are nondeterministic ([DATEPART][m-datepart], [CAST and CONVERT][m-cast]) | Both |

## 5. NULL handling

| ID | Example | Vocabulary | T-SQL | Evidence |
|---|---|---|---|---|
| 5.1 | `CONCAT('a', NULL, 'b')` | `concat_null_as_empty` | `'ab'`: `CONCAT` converts `NULL` to an empty string, and all-`NULL` input gives `''` ([CONCAT][m-concat]) | Both |
| 5.2 | `'abc' + NULL` | `concat_null_propagating` | `NULL`. Under the deprecated `CONCAT_NULL_YIELDS_NULL OFF` it is `'abc'`; the setting is always `ON` from SQL Server 2017 ([CONCAT_NULL_YIELDS_NULL][m-concatnull]) | Both |
| 5.3 | `WHERE col = NULL` with `SET ANSI_NULLS OFF` | `compare_null_as_value` | Returns the rows where `col` is `NULL`; `<> NULL` returns the non-null rows. With `ON` both return no rows ([ANSI_NULLS][m-ansinulls]) | Both |
| 5.4 | `ISNULL(CAST(NULL AS varchar(2)), 'abcdef')` | `null_replacement` | `'ab'`: the result has the type of the first argument and the replacement can be truncated ([ISNULL][m-isnull]) | Both |
| 5.5 | `ISNULL(int_col, 1.5)` | `null_replacement` | `1` (derived): the replacement is converted to `int`, which truncates ([ISNULL][m-isnull], [CAST and CONVERT][m-cast]) | Both |
| 5.6 | `COALESCE(varchar_col, 0)` | `first_non_null` | `int`, by data type precedence ([COALESCE][m-coalesce]) | Both |

## 6. Row counts and identity

| ID | Example | Vocabulary | T-SQL | Evidence |
|---|---|---|---|---|
| 6.1 | `UPDATE …; IF @@ROWCOUNT = 0 …` | `rows_affected` | `@@ROWCOUNT` is set by almost every statement: rows affected for DML, rows read for `SELECT`, `1` for `SET @v`, `0` after `PRINT`, `USE` or `BEGIN TRANSACTION` ([@@ROWCOUNT][m-rowcount]) | Both |
| 6.2 | `MERGE …; SELECT @@ROWCOUNT` | `rows_affected` after `merge_rows` | The total of rows inserted, updated and deleted ([MERGE][m-merge]) | Both |
| 6.3 | `id int IDENTITY(1, 1)` and code that assumes dense values | `insert_rows` (identity assigned on write) | Each value is generated from the current seed and increment, on any integer type. Consecutive values are not guaranteed: concurrent inserts, failed or rolled-back statements and restarts leave gaps ([IDENTITY][m-identity]) | Both |
| 6.4 | `SCOPE_IDENTITY()`, `@@IDENTITY`, `IDENT_CURRENT` | `last_identity` (scope) | The last identity value generated in the scope, session or table ([SCOPE_IDENTITY][m-scopeidentity], [@@IDENTITY][m-atidentity]) | Inferred |
| 6.5 | `SELECT * INTO new_table FROM t` where `t` has an identity column | `create_table_from_query` | The new column inherits the `IDENTITY` property ([SELECT INTO][m-selectinto]) | Both |

## 7. Errors and transactions

| ID | Example | Vocabulary | T-SQL | Evidence |
|---|---|---|---|---|
| 7.1 | `BEGIN TRAN; INSERT a …; INSERT b …; ROLLBACK` | `begin_transaction`, `rollback_transaction` | Every statement in the transaction is undone ([transaction guide][m-txguide]) | Both |
| 7.2 | Three inserts, the second violating a constraint, `XACT_ABORT OFF`, no `TRY` | `set_abort_on_error`, `last_statement_error` | Only the failing statement is rolled back; the batch continues and the third insert runs ([XACT_ABORT][m-xactabort], [transaction guide][m-txguide]) | Inferred |
| 7.3 | The same with `SET XACT_ABORT ON` | `set_abort_on_error` | The whole transaction is rolled back and the batch ends ([XACT_ABORT][m-xactabort]) | Both |
| 7.4 | An explicit transaction with `XACT_ABORT OFF` where one statement fails and the code then commits | `transaction_state`, `commit_transaction` | The failing statement is rolled back and the transaction can continue and commit the rest ([XACT_ABORT][m-xactabort]) | Both |
| 7.5 | `INSERT` of a duplicate key into a table with a primary key or unique constraint, often inside `TRY…CATCH` as an upsert | `insert_rows`, `try_catch`, `error_field` | The insert fails and the error is caught ([TRY...CATCH][m-trycatch]) | Both |
| 7.6 | `BEGIN TRY … END TRY BEGIN CATCH … END CATCH` | `try_catch` | Catches errors of severity above 10 that do not close the connection. Does not catch warnings, or compile errors at the same execution level ([TRY...CATCH][m-trycatch]) | Inferred |

## 8. MERGE

| ID | Example | Vocabulary | T-SQL | Evidence |
|---|---|---|---|---|
| 8.1 | `MERGE … ON t.code = s.code` with string keys | `merge_rows` with a comparison rule | Keys match ignoring case and trailing spaces (rows 2.1, 2.4), so the row is updated | Inferred |
| 8.2 | `UPDATE t SET … FROM t JOIN s …` where several `s` rows match one `t` row | `update_rows` | No error; which source row is used is undefined ([UPDATE][m-update]) | Both |
| 8.3 | `MERGE … OUTPUT $action, inserted.*, deleted.*` | `return_changed_rows` | Returns one row for every row inserted, updated or deleted ([MERGE][m-merge], [OUTPUT][m-output]) | Inferred |
| 8.4 | A `MERGE` target that has triggers | `merge_rows` (triggers fire on the write) | `AFTER` and `INSTEAD OF` triggers fire for each action ([MERGE][m-merge]) | Inferred |
| 8.5 | `MERGE` with one unconditional `WHEN MATCHED THEN DELETE` and several source rows per target row | `merge_rows` | `MERGE` cannot update the same row more than once, or update and delete the same row; the page does not state the delete-only case ([MERGE][m-merge]) | Inferred |

## 9. String functions, TOP and other statements

| ID | Example | Vocabulary | T-SQL | Evidence |
|---|---|---|---|---|
| 9.1 | `LEN('ab  ')` | `length_without_trailing_spaces` | `2`: trailing spaces are excluded ([LEN][m-len]) | Both |
| 9.2 | `DATALENGTH(N'abc')` | `byte_length` | `6` (derived): bytes, and `nvarchar` uses byte-pairs ([DATALENGTH][m-datalength], [precision, scale and length][m-precision]) | Both |
| 9.3 | `SUBSTRING('abcdef', -1, 3)` | `substring` | `'a'` (derived): a start below 1 begins at the first character and returns `start + length - 1` characters ([SUBSTRING][m-substring]) | Both |
| 9.4 | `SUBSTRING('abcdef', 0, 3)` | `substring` | `'ab'` (derived, same rule) ([SUBSTRING][m-substring]) | Inferred |
| 9.5 | `CHARINDEX('b', 'abc', 0)` | `position_of` | `2`: a start of zero or below searches from the beginning ([CHARINDEX][m-charindex]) | Both |
| 9.6 | `LEFT('abc', -1)`, `RIGHT('abc', -1)` | `left_part`, `right_part` | Error ([LEFT][m-left]) | Both |
| 9.7 | `STRING_SPLIT('a.b.c', '.')` | `split_text` | Rows `a`, `b`, `c`, in no guaranteed order unless the ordinal column is requested ([STRING_SPLIT][m-stringsplit]) | Both |
| 9.8 | `STUFF('abc', 0, 1, 'x')` | `overlay` | `NULL` when the start is zero, negative or past the end, or the length is negative ([STUFF][m-stuff]) | Inferred |
| 9.9 | `SELECT TOP 10 * FROM t` without `ORDER BY` | `limit` with no order | Ten rows "in an undefined order" ([TOP][m-top]) | Both |
| 9.10 | `SELECT TOP 5 WITH TIES … ORDER BY x` | `limit`, `rank` | Also returns rows that tie with the last row ([TOP][m-top]) | Inferred |
| 9.11 | `SELECT … INTO #tmp FROM src`, then `src` is modified, then `#tmp` is read | `create_temp_table` from a query (a snapshot) | `#tmp` is a table holding the rows as they were ([SELECT INTO][m-selectinto]) | Both |
| 9.12 | `UPDATE t SET x = RAND()` | `random_number` | Every affected row gets the same value ([RAND][m-rand]) | Both |
| 9.13 | `SELECT @v = col FROM t` | `assign_from_rows` | Several rows: `@v` gets the last value returned. No rows: `@v` keeps its previous value ([SELECT @local_variable][m-selectvar]) | Both |
| 9.14 | `'C:\temp\new'` inside embedded SQL text (the escape hatch, `expr`, `selectExpr`) | None: this is about target-side SQL text in the escape hatch, not a source construct | Backslash is an ordinary character: the constants page defines no backslash escape sequences ([constants][m-constants]) | Inferred |

## Sources

Microsoft T-SQL reference:

[m-add]: https://learn.microsoft.com/en-us/sql/t-sql/language-elements/add-transact-sql
[m-ansinulls]: https://learn.microsoft.com/en-us/sql/t-sql/statements/set-ansi-nulls-transact-sql
[m-ansipadding]: https://learn.microsoft.com/en-us/sql/t-sql/statements/set-ansi-padding-transact-sql
[m-arithabort]: https://learn.microsoft.com/en-us/sql/t-sql/statements/set-arithabort-transact-sql
[m-atidentity]: https://learn.microsoft.com/en-us/sql/t-sql/functions/identity-transact-sql
[m-avg]: https://learn.microsoft.com/en-us/sql/t-sql/functions/avg-transact-sql
[m-cast]: https://learn.microsoft.com/en-us/sql/t-sql/functions/cast-and-convert-transact-sql
[m-charindex]: https://learn.microsoft.com/en-us/sql/t-sql/functions/charindex-transact-sql
[m-charvarchar]: https://learn.microsoft.com/en-us/sql/t-sql/data-types/char-and-varchar-transact-sql
[m-coalesce]: https://learn.microsoft.com/en-us/sql/t-sql/language-elements/coalesce-transact-sql
[m-collation]: https://learn.microsoft.com/en-us/sql/relational-databases/collations/collation-and-unicode-support
[m-concat]: https://learn.microsoft.com/en-us/sql/t-sql/functions/concat-transact-sql
[m-concatnull]: https://learn.microsoft.com/en-us/sql/t-sql/statements/set-concat-null-yields-null-transact-sql
[m-concatws]: https://learn.microsoft.com/en-us/sql/t-sql/functions/concat-ws-transact-sql
[m-constants]: https://learn.microsoft.com/en-us/sql/t-sql/data-types/constants-transact-sql
[m-conversion]: https://learn.microsoft.com/en-us/sql/t-sql/data-types/data-type-conversion-database-engine
[m-datalength]: https://learn.microsoft.com/en-us/sql/t-sql/functions/datalength-transact-sql
[m-dateadd]: https://learn.microsoft.com/en-us/sql/t-sql/functions/dateadd-transact-sql
[m-datediff]: https://learn.microsoft.com/en-us/sql/t-sql/functions/datediff-transact-sql
[m-datefirst]: https://learn.microsoft.com/en-us/sql/t-sql/statements/set-datefirst-transact-sql
[m-datepart]: https://learn.microsoft.com/en-us/sql/t-sql/functions/datepart-transact-sql
[m-datetime]: https://learn.microsoft.com/en-us/sql/t-sql/data-types/datetime-transact-sql
[m-datetime2]: https://learn.microsoft.com/en-us/sql/t-sql/data-types/datetime2-transact-sql
[m-datetimeoffset]: https://learn.microsoft.com/en-us/sql/t-sql/data-types/datetimeoffset-transact-sql
[m-decimal]: https://learn.microsoft.com/en-us/sql/t-sql/data-types/decimal-and-numeric-transact-sql
[m-distinct]: https://learn.microsoft.com/en-us/sql/t-sql/queries/select-clause-transact-sql
[m-divide]: https://learn.microsoft.com/en-us/sql/t-sql/language-elements/divide-transact-sql
[m-getdate]: https://learn.microsoft.com/en-us/sql/t-sql/functions/getdate-transact-sql
[m-identity]: https://learn.microsoft.com/en-us/sql/t-sql/statements/create-table-transact-sql-identity-property
[m-int]: https://learn.microsoft.com/en-us/sql/t-sql/data-types/int-bigint-smallint-and-tinyint-transact-sql
[m-isnull]: https://learn.microsoft.com/en-us/sql/t-sql/functions/isnull-transact-sql
[m-isnumeric]: https://learn.microsoft.com/en-us/sql/t-sql/functions/isnumeric-transact-sql
[m-left]: https://learn.microsoft.com/en-us/sql/t-sql/functions/left-transact-sql
[m-len]: https://learn.microsoft.com/en-us/sql/t-sql/functions/len-transact-sql
[m-like]: https://learn.microsoft.com/en-us/sql/t-sql/language-elements/like-transact-sql
[m-merge]: https://learn.microsoft.com/en-us/sql/t-sql/statements/merge-transact-sql
[m-money]: https://learn.microsoft.com/en-us/sql/t-sql/data-types/money-and-smallmoney-transact-sql
[m-orderby]: https://learn.microsoft.com/en-us/sql/t-sql/queries/select-order-by-clause-transact-sql
[m-output]: https://learn.microsoft.com/en-us/sql/t-sql/queries/output-clause-transact-sql
[m-power]: https://learn.microsoft.com/en-us/sql/t-sql/functions/power-transact-sql
[m-precedence]: https://learn.microsoft.com/en-us/sql/t-sql/data-types/data-type-precedence-transact-sql
[m-precision]: https://learn.microsoft.com/en-us/sql/t-sql/data-types/precision-scale-and-length-transact-sql
[m-rand]: https://learn.microsoft.com/en-us/sql/t-sql/functions/rand-transact-sql
[m-replace]: https://learn.microsoft.com/en-us/sql/t-sql/functions/replace-transact-sql
[m-round]: https://learn.microsoft.com/en-us/sql/t-sql/functions/round-transact-sql
[m-rowcount]: https://learn.microsoft.com/en-us/sql/t-sql/functions/rowcount-transact-sql
[m-scopeidentity]: https://learn.microsoft.com/en-us/sql/t-sql/functions/scope-identity-transact-sql
[m-selectinto]: https://learn.microsoft.com/en-us/sql/t-sql/queries/select-into-clause-transact-sql
[m-selectvar]: https://learn.microsoft.com/en-us/sql/t-sql/language-elements/select-local-variable-transact-sql
[m-smalldatetime]: https://learn.microsoft.com/en-us/sql/t-sql/data-types/smalldatetime-transact-sql
[m-strcmp]: https://learn.microsoft.com/en-us/sql/t-sql/language-elements/string-comparison-assignment
[m-stringagg]: https://learn.microsoft.com/en-us/sql/t-sql/functions/string-agg-transact-sql
[m-stringconcat]: https://learn.microsoft.com/en-us/sql/t-sql/language-elements/string-concatenation-transact-sql
[m-stringsplit]: https://learn.microsoft.com/en-us/sql/t-sql/functions/string-split-transact-sql
[m-stuff]: https://learn.microsoft.com/en-us/sql/t-sql/functions/stuff-transact-sql
[m-substring]: https://learn.microsoft.com/en-us/sql/t-sql/functions/substring-transact-sql
[m-sum]: https://learn.microsoft.com/en-us/sql/t-sql/functions/sum-transact-sql
[m-sysdatetime]: https://learn.microsoft.com/en-us/sql/t-sql/functions/sysdatetime-transact-sql
[m-top]: https://learn.microsoft.com/en-us/sql/t-sql/queries/top-transact-sql
[m-trim]: https://learn.microsoft.com/en-us/sql/t-sql/functions/trim-transact-sql
[m-trycast]: https://learn.microsoft.com/en-us/sql/t-sql/functions/try-cast-transact-sql
[m-trycatch]: https://learn.microsoft.com/en-us/sql/t-sql/language-elements/try-catch-transact-sql
[m-txguide]: https://learn.microsoft.com/en-us/sql/relational-databases/sql-server-transaction-locking-and-row-versioning-guide
[m-update]: https://learn.microsoft.com/en-us/sql/t-sql/queries/update-transact-sql
[m-xactabort]: https://learn.microsoft.com/en-us/sql/t-sql/statements/set-xact-abort-transact-sql

- Language elements: [+ operator][m-add], [division][m-divide], [string concatenation][m-stringconcat], [string comparison][m-strcmp], [LIKE][m-like], [COALESCE][m-coalesce], [SELECT @local_variable][m-selectvar], [TRY...CATCH][m-trycatch]
- Data types: [precedence][m-precedence], [conversion][m-conversion], [precision, scale and length][m-precision], [integer types][m-int], [decimal and numeric][m-decimal], [money][m-money], [char and varchar][m-charvarchar], [constants][m-constants], [datetime][m-datetime], [datetime2][m-datetime2], [smalldatetime][m-smalldatetime], [datetimeoffset][m-datetimeoffset]
- Functions: [CAST and CONVERT][m-cast], [TRY_CAST][m-trycast], [AVG][m-avg], [SUM][m-sum], [POWER][m-power], [ROUND][m-round], [RAND][m-rand], [ISNULL][m-isnull], [ISNUMERIC][m-isnumeric], [CONCAT][m-concat], [CONCAT_WS][m-concatws], [LEN][m-len], [DATALENGTH][m-datalength], [SUBSTRING][m-substring], [CHARINDEX][m-charindex], [LEFT][m-left], [REPLACE][m-replace], [STUFF][m-stuff], [STRING_SPLIT][m-stringsplit], [STRING_AGG][m-stringagg], [TRIM][m-trim], [DATEDIFF][m-datediff], [DATEADD][m-dateadd], [DATEPART][m-datepart], [GETDATE][m-getdate], [SYSDATETIME][m-sysdatetime], [@@ROWCOUNT][m-rowcount], [@@IDENTITY][m-atidentity], [SCOPE_IDENTITY][m-scopeidentity]
- Statements and queries: [MERGE][m-merge], [OUTPUT][m-output], [UPDATE][m-update], [TOP][m-top], [ORDER BY][m-orderby], [SELECT clause][m-distinct], [SELECT INTO][m-selectinto], [IDENTITY property][m-identity], [SET ANSI_NULLS][m-ansinulls], [SET ANSI_PADDING][m-ansipadding], [SET ARITHABORT][m-arithabort], [SET CONCAT_NULL_YIELDS_NULL][m-concatnull], [SET DATEFIRST][m-datefirst], [SET XACT_ABORT][m-xactabort]
- Guides: [collation and Unicode support][m-collation], [transaction locking and row versioning guide][m-txguide]

Apache Spark and PySpark:

[s-ansi]: https://spark.apache.org/docs/latest/sql-ref-ansi-compliance.html
[s-config]: https://spark.apache.org/docs/latest/configuration.html
[s-fdatetime]: https://spark.apache.org/docs/latest/api/sql/datetime-functions/
[s-fmath]: https://spark.apache.org/docs/latest/api/sql/math-functions/
[s-fstring]: https://spark.apache.org/docs/latest/api/sql/string-functions/
[s-ident]: https://spark.apache.org/docs/latest/sql-ref-identifier.html
[s-like]: https://spark.apache.org/docs/latest/sql-ref-syntax-qry-select-like.html
[s-limit]: https://spark.apache.org/docs/latest/sql-ref-syntax-qry-select-limit.html
[s-literals]: https://spark.apache.org/docs/latest/sql-ref-literals.html
[s-migration]: https://spark.apache.org/docs/latest/sql-migration-guide.html
[s-null]: https://spark.apache.org/docs/latest/sql-ref-null-semantics.html
[s-orderby]: https://spark.apache.org/docs/latest/sql-ref-syntax-qry-select-orderby.html
[s-scripting]: https://spark.apache.org/docs/latest/sql-ref-scripting.html
[s-types]: https://spark.apache.org/docs/latest/sql-ref-datatypes.html
[p-datediff]: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.functions.datediff.html
[p-eqnullsafe]: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.Column.eqNullSafe.html
[p-first]: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.DataFrame.first.html
[p-mono]: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.functions.monotonically_increasing_id.html
[p-quick]: https://spark.apache.org/docs/latest/api/python/getting_started/quickstart_df.html
[p-rand]: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.functions.rand.html
[p-round]: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.functions.round.html
[p-substring]: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.functions.substring.html
[p-tempview]: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.DataFrame.createOrReplaceTempView.html
[p-tsdiff]: https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql/api/pyspark.sql.functions.timestamp_diff.html

- SQL reference: [ANSI compliance][s-ansi], [NULL semantics][s-null], [data types][s-types], [literals][s-literals], [identifiers][s-ident], [ORDER BY][s-orderby], [LIMIT][s-limit], [LIKE][s-like], [SQL scripting][s-scripting], [migration guide][s-migration], [configuration][s-config]
- Built-in functions: [string][s-fstring], [datetime][s-fdatetime], [math][s-fmath]
- PySpark: [quickstart][p-quick], [`substring`][p-substring], [`datediff`][p-datediff], [`timestamp_diff`][p-tsdiff], [`round`][p-round], [`rand`][p-rand], [`monotonically_increasing_id`][p-mono], [`DataFrame.first`][p-first], [`Column.eqNullSafe`][p-eqnullsafe], [`createOrReplaceTempView`][p-tempview]

Databricks:

[d-acid]: https://docs.databricks.com/aws/en/lakehouse/acid
[d-ansi]: https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-ansi-compliance
[d-ansimode]: https://docs.databricks.com/aws/en/sql/language-manual/parameters/ansi_mode
[d-avg]: https://docs.databricks.com/aws/en/sql/language-manual/functions/avg
[d-cast]: https://docs.databricks.com/aws/en/sql/language-manual/functions/cast
[d-charindex]: https://docs.databricks.com/aws/en/sql/language-manual/functions/charindex
[d-collation]: https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-collation
[d-compound]: https://docs.databricks.com/aws/en/sql/language-manual/control-flow/compound-stmt
[d-constraints]: https://docs.databricks.com/aws/en/tables/constraints
[d-datediff3]: https://docs.databricks.com/aws/en/sql/language-manual/functions/datediff3
[d-decimal]: https://docs.databricks.com/aws/en/sql/language-manual/data-types/decimal-type
[d-div]: https://docs.databricks.com/aws/en/sql/language-manual/functions/div
[d-gencols]: https://docs.databricks.com/aws/en/tables/features/generated-columns
[d-getdiag]: https://docs.databricks.com/aws/en/sql/language-manual/control-flow/get-diagnostics-stmt
[d-history]: https://docs.databricks.com/aws/en/tables/history
[d-ident]: https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-identifiers
[d-like]: https://docs.databricks.com/aws/en/sql/language-manual/functions/like
[d-limit]: https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-qry-select-limit
[d-merge]: https://docs.databricks.com/aws/en/sql/language-manual/delta-merge-into
[d-plus]: https://docs.databricks.com/aws/en/sql/language-manual/functions/plussign
[d-pow]: https://docs.databricks.com/aws/en/sql/language-manual/functions/pow
[d-round]: https://docs.databricks.com/aws/en/sql/language-manual/functions/round
[d-slash]: https://docs.databricks.com/aws/en/sql/language-manual/functions/slashsign
[d-split]: https://docs.databricks.com/aws/en/sql/language-manual/functions/split
[d-stringagg]: https://docs.databricks.com/aws/en/sql/language-manual/functions/string_agg
[d-substr]: https://docs.databricks.com/aws/en/sql/language-manual/functions/substr
[d-sum]: https://docs.databricks.com/aws/en/sql/language-manual/functions/sum
[d-timestamp]: https://docs.databricks.com/aws/en/sql/language-manual/data-types/timestamp-type
[d-timezone]: https://docs.databricks.com/aws/en/sql/language-manual/parameters/timezone
[d-tinyint]: https://docs.databricks.com/aws/en/sql/language-manual/data-types/tinyint-type
[d-txn]: https://docs.databricks.com/aws/en/transactions/
[d-typerules]: https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-datatype-rules
[d-update]: https://docs.databricks.com/aws/en/sql/language-manual/delta-update
[d-weekofyear]: https://docs.databricks.com/aws/en/sql/language-manual/functions/weekofyear

- Modes and types: [ANSI_MODE][d-ansimode], [ANSI compliance in Databricks Runtime][d-ansi], [SQL data type rules][d-typerules], [collation][d-collation], [identifiers][d-ident], [DECIMAL][d-decimal], [TINYINT][d-tinyint], [TIMESTAMP][d-timestamp], [TIMEZONE][d-timezone]
- Functions and operators: [`cast`][d-cast], [`/`][d-slash], [`div`][d-div], [`+`][d-plus], [`avg`][d-avg], [`sum`][d-sum], [`pow`][d-pow], [`round`][d-round], [`substr`][d-substr], [`charindex`][d-charindex], [`like`][d-like], [`split`][d-split], [`string_agg`][d-stringagg], [`datediff` (timestamp)][d-datediff3], [`weekofyear`][d-weekofyear], [LIMIT][d-limit]
- Tables and transactions: [MERGE INTO][d-merge], [UPDATE][d-update], [identity columns][d-gencols], [constraints][d-constraints], [table history][d-history], [ACID guarantees][d-acid], [transactions][d-txn], [compound statement][d-compound], [GET DIAGNOSTICS][d-getdiag]

Delta Lake:

[dl-batch]: https://docs.delta.io/delta-batch/
[dl-cc]: https://docs.delta.io/concurrency-control/
[dl-update]: https://docs.delta.io/delta-update/
[dl-utility]: https://docs.delta.io/delta-utility/

- [Table batch reads and writes (identity columns)][dl-batch], [concurrency control][dl-cc], [table deletes, updates and merges][dl-update], [table utility commands (history metrics)][dl-utility]
