# Annex D. Silent differences: the PySpark target half

Part of the [v1 architecture spec](v1-architecture-spec.md). This half says where Spark's built-ins differ from the meaning SQL Server gives the construct, and which built-in, if any, reproduces it. The support library, comparison tolerances and mutation testing use it.

Entries are copied unchanged from [the research catalogue](../research/tsql-spark-silent-differences.md), the output of [Where do T-SQL and Spark silently disagree?](https://github.com/Bach-To-The-Future/data-migration/issues/6), and keep its IDs, so an entry's two halves can be read side by side. The split follows [ADR 0019](../adr/0019-shared-mappings-are-split-by-source-and-by-target.md). Sections 10 (behaviours that agree) and 11 (what could not be established) are in [Annex G](annex-g-agreements-and-tested-differences.md); the research catalogue keeps its method and evidence labels.

Fourteen entries were marked "Inferred" from documentation; thirteen were later confirmed on SQL Server and local Spark, and entry 8.5 was refuted (no difference). Those results are in [Annex G](annex-g-agreements-and-tested-differences.md), not in the entries below.


## 1. ANSI mode

| ID | Example | Spark, ANSI on | Spark, ANSI off | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|---|
| 1.1 | `SELECT 2147483647 + 1` | `ARITHMETIC_OVERFLOW` error ([Spark ANSI][s-ansi]) | `-2147483648` ([Spark ANSI][s-ansi]) | ANSI on | Both |
| 1.2 | `SELECT 1 / 0` | `DIVIDE_BY_ZERO` error ([ANSI_MODE][d-ansimode]) | `NULL` ([ANSI_MODE][d-ansimode]) | ANSI on. `try_divide` gives the `NULL` variant in both modes ([Spark ANSI][s-ansi]) | Both |
| 1.3 | `CAST('a' AS int)` | `CAST_INVALID_INPUT` error ([Spark ANSI][s-ansi]) | `NULL` ([Spark ANSI][s-ansi]) | ANSI on. T-SQL `TRY_CAST` ([TRY_CAST][m-trycast]) maps to `try_cast` in both modes | Both |
| 1.4 | `CAST(12345 AS tinyint)` | `CAST_OVERFLOW` error ([ANSI_MODE][d-ansimode]) | `57` ([ANSI_MODE][d-ansimode]) | ANSI on | Both |
| 1.5 | `10 = '10.1'` (integer against string) | The string is promoted to `BIGINT`; runtime error ([ANSI_MODE][d-ansimode]) | `true` ([ANSI_MODE][d-ansimode]) | ANSI on | Both |
| 1.6 | `'5' + '7.6'` (two strings) | Type-mismatch error ([ANSI_MODE][d-ansimode]) | `12.6`: both strings are cast to `DOUBLE` ([ANSI_MODE][d-ansimode]) | `concat`, chosen by the converter from the operand types. No setting | Both |
| 1.7 | `SELECT 5 UNION ALL SELECT '6'` (also `CASE` and `COALESCE` with mixed branches) | `BIGINT` column ([ANSI_MODE][d-ansimode]) | `STRING` column, so later sorting and comparison are lexical ([ANSI_MODE][d-ansimode]) | Explicit cast of each branch | Both |
| 1.8 | A statement in `BEGIN TRY` that overflows, divides by zero or fails a cast | An exception is raised, so a converted handler runs | No exception; the handler never runs and the `NULL` or wrapped value flows on (rows 1.1 to 1.4) | ANSI on | Both |

## 2. Collation, case sensitivity and trailing spaces

| ID | Example | Spark (default collation) | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|
| 2.1 | `'abc' = 'ABC'` | False: `'A' <> 'a'` ([Databricks collation][d-collation]) | `COLLATE UTF8_LCASE` or `UNICODE_CI`, the `collate` function, or a default collation on the table, schema or session (Databricks SQL, Runtime 16.1+) ([Databricks collation][d-collation]) | Both |
| 2.2 | The same values as keys in `JOIN`, `GROUP BY`, `DISTINCT`, `IN`, set operators and `MERGE … ON` | They do not match and form separate groups | As 2.1, applied to the key columns | Inferred |
| 2.3 | `'Cafe' = 'Café'` | False. `UTF8_LCASE` does not fold accents ([Databricks collation][d-collation]) | `UNICODE_CI_AI` for accent-insensitive sources ([Databricks collation][d-collation]) | Both |
| 2.4 | `'abc' = 'abc '` | False ([Databricks collation][d-collation]) | The `RTRIM` modifier, for example `UTF8_BINARY_RTRIM` or `UTF8_LCASE_RTRIM` (Databricks SQL, Runtime 16.2+) ([Databricks collation][d-collation]); otherwise `rtrim` on both sides | Both |
| 2.5 | `'' = '  '`, so `WHERE col = ''` | False | As 2.4 | Both |
| 2.6 | `'abc ' LIKE 'abc'` on `varchar` | False: `LIKE` is always sensitive to trailing spaces, even under an `RTRIM` collation ([Databricks `like`][d-like]) | None. Apply `rtrim` to the matched expression when the source column is `char` or `varchar` | Both |
| 2.7 | `ORDER BY`, `MIN`, `MAX`, `<`, `BETWEEN` on strings such as `'apple'` and `'Banana'` | Byte order: `'Z' < 'a'`, so `'Banana'` sorts first ([Databricks collation][d-collation]) | `UTF8_LCASE` or `UNICODE_CI` ([Databricks collation][d-collation]). Exact equality with a given SQL Server sort order is not established (§11) | Both |
| 2.8 | `'ABC' LIKE 'a%'` | False | `ILIKE`, or `LIKE` on a `UTF8_LCASE` string. `LIKE` supports only `UTF8_BINARY` and `UTF8_LCASE`, with or without `RTRIM` ([Databricks `like`][d-like], [Databricks collation][d-collation]) | Both |
| 2.9 | `'apple' LIKE '[a-c]%'` | False: only `%` and `_` are special, every other character is literal ([Spark LIKE][s-like], [Databricks `like`][d-like]) | `RLIKE` with the pattern rewritten as a regular expression | Both |
| 2.10 | `col LIKE 'a\_b'` | `\` is the default escape character, so `\_` matches only a literal underscore ([Spark LIKE][s-like]) | An explicit `ESCAPE` clause naming a character the pattern does not use | Both |
| 2.11 | `REPLACE('ABCabc', 'abc', 'x')`; also `CHARINDEX` | `'ABCx'`: the documented example `replace('ABCabc', 'abc', 'DEF')` returns `'ABCDEF'` ([Spark string functions][s-fstring]) | Not established whether these functions honour a non-binary collation (§11) | Both |
| 2.12 | `CAST('ab' AS char(5)) + '!'` | `'ab!'`: `CAST` treats `CHAR` and `VARCHAR` as `STRING`; only table columns pad ([Spark migration guide][s-migration], [Spark data types][s-types]) | `rpad(x, n)` | Both |
| 2.13 | `CAST('abcdef' AS varchar(3))`; `CAST(x AS varchar)` (length 30); `DECLARE @v varchar` (length 1) | Not truncated: `CAST` ignores the length ([Spark migration guide][s-migration]) | `substring(x, 1, n)` or `left(x, n)` | Both |

## 3. Numbers: division, implicit conversion, decimal precision

| ID | Example | Spark | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|
| 3.1 | `5 / 2` on integers | `2.5`: `/` returns `DOUBLE` unless both operands are `DECIMAL` ([Databricks `/`][d-slash]) | `div`, which returns the integral part as `BIGINT` and truncates toward zero (`-5.9 div 1` is `-5`) ([Databricks `div`][d-div], [Spark math functions][s-fmath]) | Both |
| 3.2 | `AVG(c)` over an `int` column holding 1 and 2 | `1.5`: the result is `DOUBLE` ([Databricks `avg`][d-avg]) | `sum(c) div count(c)` | Both |
| 3.3 | `AVG(c)` over `decimal(p, s)` | `DECIMAL(p + 4, s + 4)` ([Databricks `avg`][d-avg]) | Cast the result to the T-SQL type. Whether the last digit then agrees is not established (§11) | Both |
| 3.4 | `SUM(c)` | `BIGINT` for any integral input; `DECIMAL(p + min(10, 31-p), s)` for decimal input. ANSI off: a `BIGINT` overflow wraps ([Databricks `sum`][d-sum]) | Cast the result to the T-SQL type | Both |
| 3.5 | `POWER(2, -100.0)` | About `7.9E-31`: `pow` returns `DOUBLE` ([Databricks `pow`][d-pow]) | Cast the result to the first argument's type | Both |
| 3.6 | `ROUND(123.456, 2, 1)` | `round` takes two arguments, always rounds half up and reduces the scale ([Databricks `round`][d-round]) | None found. A converter that drops the third argument rounds where T-SQL truncated | Both |
| 3.7 | `tinyint` value `200` | `TINYINT` is -128 to 127 ([Databricks TINYINT][d-tinyint]). ANSI on: overflow error. ANSI off: wraps | Map `tinyint` to `SMALLINT` | Both |
| 3.8 | `CAST(12345678901 AS decimal)` | Default is `DECIMAL(10, 0)` ([Databricks DECIMAL][d-decimal]). ANSI on: overflow error. ANSI off: no error | Always emit an explicit precision and scale | Both |
| 3.9 | `CAST('' AS int)` | ANSI on: `CAST_INVALID_INPUT`. ANSI off: `NULL` ([Databricks `cast`][d-cast]) | None. Emit an explicit test for the empty string | Both |
| 3.10 | `CAST(CAST(1234567.891 AS float) AS varchar(30))` | Plain notation, without an exponent, for magnitudes from 0.001 up to 10,000,000 ([Databricks `cast`][d-cast]) | None. Format explicitly | Both |
| 3.11 | `money` arithmetic, for example `money / money` | Spark has no `money` type; as `DECIMAL(19, 4)` a division follows the decimal scale rules and keeps more digits ([Spark ANSI][s-ansi]) | Cast each intermediate result to `DECIMAL(19, 4)` | Inferred |
| 3.12 | `ISNUMERIC('$')` | No equivalent function. An emulation such as `try_cast(x AS DOUBLE) IS NOT NULL` gives a different answer for these inputs | None | Inferred |
| 3.13 | `SUM(c)` or `AVG(c)` over a `float` or `real` column | The same: partial sums per partition are combined in an order that depends on partitioning | None. Reconciliation applies a tolerance only to output columns whose lineage includes a floating-point aggregation, and names this entry in the evidence | Inferred. Added by the owner in [How does reconciliation generate data and compare results?](https://github.com/Bach-To-The-Future/data-migration/issues/13) |

## 4. Dates and times

| ID | Example | Spark | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|
| 4.1 | `CAST('2024-01-01 23:59:59.999' AS datetime)` | The timestamp keeps `.999`: six fractional digits are stored and nothing is rounded to a coarser grid ([Databricks TIMESTAMP][d-timestamp]) | None. Matters for end-of-day predicates and for comparing against stored `datetime` values | Both |
| 4.2 | `CAST('2024-05-09 23:59:59' AS smalldatetime)` | The timestamp keeps the seconds ([Databricks TIMESTAMP][d-timestamp]) | None. Emulate by adding 30 seconds and truncating to the minute | Both |
| 4.3 | `datetime2(7)`, `time(7)`, `datetimeoffset(7)`, `SYSDATETIME()` | Six fractional digits by default; extra digits in a string or cast are truncated. Databricks `TIMESTAMP(p)` with 7 to 9 digits is in Beta ([Databricks TIMESTAMP][d-timestamp], [Databricks `cast`][d-cast]) | None outside the Beta type | Both |
| 4.4 | `datetimeoffset` value `2024-05-08 12:35:29 +12:15` | `TIMESTAMP` is an absolute instant shown in the session time zone; the offset is not kept ([Spark data types][s-types]) | None. Carry the offset in a separate column | Both |
| 4.5 | `datetime` or `datetime2` mapped to Spark `TIMESTAMP` | `TIMESTAMP` is interpreted in the session time zone, so the same stored value reads differently when the session zone changes; `TIMESTAMP_NTZ` ignores time zones ([Spark data types][s-types]) | `TIMESTAMP_NTZ` | Both |
| 4.6 | `GETDATE()`, `CAST(GETDATE() AS date)` | `current_timestamp()` in the session time zone, which defaults to UTC in Databricks SQL and to the JVM's zone in Apache Spark ([Databricks TIMEZONE][d-timezone], [Spark configuration][s-config]) | Set the session time zone to the source server's zone | Both |
| 4.7 | `DATEDIFF(year, '2005-12-31 23:59:59.9999999', '2006-01-01 00:00:00.0000000')` | `0` (derived): `datediff(unit, start, end)` and `timestamp_diff` count whole elapsed units ([Databricks `datediff` (timestamp)][d-datediff3], [PySpark `timestamp_diff`][p-tsdiff]) | None single. Year: `year(end) - year(start)`. Month: year difference times 12 plus month difference. Day: two-argument `datediff` on dates. Smaller units: truncate both sides with `date_trunc`, then difference | Both |
| 4.8 | `DATEDIFF(day, @start, @end)` | Two-argument `datediff(endDate, startDate)` takes the end first ([Spark datetime functions][s-fdatetime], [PySpark `datediff`][p-datediff]) | Swap the arguments; keeping T-SQL's order flips the sign | Both |
| 4.9 | `DATEPART(weekday, '2007-04-21')` (a Saturday) | `dayofweek` is fixed at 1 = Sunday, giving `7`; `weekday` is 0 = Monday ([Spark datetime functions][s-fdatetime]) | `dayofweek` only when the source runs with `DATEFIRST 7`; otherwise compute from it | Both |
| 4.10 | `DATEPART(week, '2021-01-01')` | `53` (derived): `weekofyear` is ISO 8601, Monday start, week 1 has more than 3 days ([Spark datetime functions][s-fdatetime], [Databricks `weekofyear`][d-weekofyear]) | None for `week`. T-SQL's `iso_week` datepart does match `weekofyear` | Both |
| 4.11 | `CAST(0 AS datetime)`, `DATEADD(day, DATEDIFF(day, 0, @d), 0)`, `CAST(@d AS int)` | A number cast to `TIMESTAMP` is seconds since 1970-01-01 UTC, and the reverse cast gives seconds; both are allowed with ANSI on ([Databricks `cast`][d-cast], [Spark ANSI][s-ansi]) | None. Rewrite the idiom (`date_trunc('DAY', d)` for the common one) | Both |
| 4.12 | `CAST('' AS datetime)` | ANSI on: error. ANSI off: `NULL` ([Databricks `cast`][d-cast]) | None | Both |
| 4.13 | `CAST('01/02/2024' AS date)` | A cast accepts only the documented `yyyy-mm-dd` forms. ANSI on: error. ANSI off: `NULL` ([Databricks TIMESTAMP][d-timestamp], [Databricks `cast`][d-cast]) | `to_date` with an explicit pattern chosen from the source's `DATEFORMAT` | Both |

## 5. NULL handling

| ID | Example | Spark | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|
| 5.1 | `CONCAT('a', NULL, 'b')` | `NULL`: `concat` is null-intolerant ([Spark NULL semantics][s-null]) | `concat_ws('', …)`, which skips `NULL` ([Spark string functions][s-fstring]) | Both |
| 5.2 | `'abc' + NULL` | `concat` and the double-pipe operator give `NULL` ([Spark NULL semantics][s-null]) | `concat` matches the default. `concat_ws('', …)` matches the legacy `OFF` setting | Both |
| 5.3 | `WHERE col = NULL` with `SET ANSI_NULLS OFF` | `=` with a `NULL` operand is always `NULL`, so no rows ([Spark NULL semantics][s-null]) | `<=>` or `Column.eqNullSafe`, or `isNull` ([Spark NULL semantics][s-null], [PySpark `eqNullSafe`][p-eqnullsafe]) | Both |
| 5.4 | `ISNULL(CAST(NULL AS varchar(2)), 'abcdef')` | `coalesce` returns `'abcdef'`: the result is the least common type ([Spark ANSI][s-ansi]) | Cast the replacement to the first argument's type, with `substring` for strings | Both |
| 5.5 | `ISNULL(int_col, 1.5)` | `coalesce` returns a decimal `1.5` ([Databricks type rules][d-typerules]) | As 5.4 | Both |
| 5.6 | `COALESCE(varchar_col, 0)` | ANSI on: `BIGINT`. ANSI off: `STRING` (row 1.7) | Explicit casts | Both |

## 6. Row counts and identity

| ID | Example | Spark | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|
| 6.1 | `UPDATE …; IF @@ROWCOUNT = 0 …` | The DataFrame API has no equivalent. Databricks has `GET DIAGNOSTICS … ROW_COUNT` for the latest DML statement, only inside a SQL compound statement (Runtime 18 LTS+) ([GET DIAGNOSTICS][d-getdiag]). Delta records per-operation counts in the table history ([Delta utility][dl-utility], [Databricks history][d-history]) | None in the DataFrame API. Re-counting with `df.count()` runs the query again and can return a different number (row 9.11) | Both |
| 6.2 | `MERGE …; SELECT @@ROWCOUNT` | Delta history reports `numTargetRowsInserted`, `numTargetRowsUpdated` and `numTargetRowsDeleted` separately ([Delta utility][dl-utility]) | Sum the three metrics | Both |
| 6.3 | `id int IDENTITY(1, 1)` and code that assumes dense values | Delta identity columns are `BIGINT` only, unique and in multiples of the step, but "not guaranteed to be contiguous" ([Databricks identity columns][d-gencols], [Delta batch][dl-batch]) | None for density. `row_number()` over an explicit ordering gives dense numbers. `monotonically_increasing_id()` is unique but not consecutive and not deterministic ([PySpark][p-mono]) | Both |
| 6.4 | `SCOPE_IDENTITY()`, `@@IDENTITY`, `IDENT_CURRENT` | No documented equivalent in Spark, Delta or Databricks. Emulating with `max(id)` reads other writers' rows and any explicitly supplied values | None | Inferred |
| 6.5 | `SELECT * INTO new_table FROM t` where `t` has an identity column | `CREATE TABLE … AS SELECT` cannot define an identity column, so the copy has plain values ([Databricks identity columns][d-gencols]) | Create the table with the identity column first, then insert | Both |

## 7. Errors and transactions

| ID | Example | Spark | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|
| 7.1 | `BEGIN TRAN; INSERT a …; INSERT b …; ROLLBACK` | By default each statement is its own transaction against one table, and nothing spans statements, so a converted "rollback" leaves the earlier writes committed ([Databricks ACID][d-acid]) | Databricks only: `BEGIN ATOMIC … END` or `BEGIN TRANSACTION … COMMIT` through SQL, on Runtime 18.0+ with Unity Catalog managed tables that have catalog commits enabled; DML and `SELECT` only, no DDL ([Databricks transactions][d-txn]). Apache Spark with Delta: none ([Delta concurrency][dl-cc]) | Both |
| 7.2 | Three inserts, the second violating a constraint, `XACT_ABORT OFF`, no `TRY` | An uncaught Python exception stops the script at the second statement, so the third never runs. This is Python's behaviour and is not stated in the Spark documentation | A SQL compound statement with a `CONTINUE HANDLER`, which resumes at the next statement ([Spark SQL scripting][s-scripting], [compound statement][d-compound]); in Python, a `try` around each statement | Inferred |
| 7.3 | The same with `SET XACT_ABORT ON` | `BEGIN ATOMIC` rolls back all changes when any statement fails ([compound statement][d-compound]) | `BEGIN ATOMIC`, within the limits in 7.1. Handlers are not allowed inside an atomic block | Both |
| 7.4 | An explicit transaction with `XACT_ABORT OFF` where one statement fails and the code then commits | An atomic block rolls back everything. For an interactive transaction the documentation says only that you must run `ROLLBACK` ([Databricks transactions][d-txn]) | None found | Both |
| 7.5 | `INSERT` of a duplicate key into a table with a primary key or unique constraint, often inside `TRY…CATCH` as an upsert | Primary key, foreign key and unique constraints "are informational only and aren't enforced", so the duplicate is inserted ([Databricks constraints][d-constraints]) | None. Rewrite as `MERGE`, or check before inserting | Both |
| 7.6 | `BEGIN TRY … END TRY BEGIN CATCH … END CATCH` | A Python `except` catches every exception, including analysis errors such as an unresolved column; a SQL `SQLEXCEPTION` handler covers every SQLSTATE outside classes `XX` and `02` ([Spark SQL scripting][s-scripting]) | None exact. The handler can run for errors T-SQL would not have routed to `CATCH` | Inferred |

## 8. MERGE

| ID | Example | Spark (Delta) | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|
| 8.1 | `MERGE … ON t.code = s.code` with string keys | The keys do not match under `UTF8_BINARY`, so `WHEN NOT MATCHED` inserts a second row | Collation on the key columns, as in §2 | Inferred |
| 8.2 | `UPDATE t SET … FROM t JOIN s …` where several `s` rows match one `t` row | The documented `UPDATE` syntax has no `FROM` clause ([Databricks UPDATE][d-update]); the natural rewrite as `MERGE` fails with `DELTA_MULTIPLE_SOURCE_ROW_MATCHING_TARGET_ROW_IN_MERGE` ([Databricks MERGE][d-merge]) | None. Deduplicate the source with an explicit rule. This is an error and not silent, but the source code may depend on the lenient behaviour | Both |
| 8.3 | `MERGE … OUTPUT $action, inserted.*, deleted.*` | The documented Delta `MERGE` syntax has no `OUTPUT` or returning clause ([Databricks MERGE][d-merge]). Reading the table before and after is not atomic with the merge | None found (§11) | Inferred |
| 8.4 | A `MERGE` target that has triggers | The documented Delta DML has no triggers, so their side effects do not happen | None | Inferred |
| 8.5 | `MERGE` with one unconditional `WHEN MATCHED THEN DELETE` and several source rows per target row | Allowed: "an unconditional delete is not ambiguous" ([Databricks MERGE][d-merge]) | Not needed; Spark succeeds with the intended result | Inferred |

## 9. String functions, TOP and other statements

| ID | Example | Spark | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|
| 9.1 | `LEN('ab  ')` | `length` returns `4`: "includes the trailing spaces" ([Spark string functions][s-fstring]) | `length(rtrim(x))` | Both |
| 9.2 | `DATALENGTH(N'abc')` | `octet_length('abc')` is `3` (derived): the byte length of the string, which Databricks stores as UTF-8 ([Spark string functions][s-fstring], [Databricks collation][d-collation]) | None. Use `length(x) * 2` for `nvarchar` without supplementary characters | Both |
| 9.3 | `SUBSTRING('abcdef', -1, 3)` | `'f'` (derived): a negative position counts from the end ([Spark string functions][s-fstring], [Databricks `substr`][d-substr]) | None. When the start can be below 1, emit `substring(x, 1, greatest(start + len - 1, 0))` | Both |
| 9.4 | `SUBSTRING('abcdef', 0, 3)` | Not documented for position 0; PySpark says only that the position is 1-based ([PySpark `substring`][p-substring]) | As 9.3 | Inferred |
| 9.5 | `CHARINDEX('b', 'abc', 0)` | `0`: "if pos is less than 1, the result is 0" ([Databricks `charindex`][d-charindex]) | `charindex` or `locate` with `greatest(start, 1)`. Note `instr(str, substr)` takes its arguments in the opposite order ([Spark string functions][s-fstring]) | Both |
| 9.6 | `LEFT('abc', -1)`, `RIGHT('abc', -1)` | Empty string ([Spark string functions][s-fstring]) | None; Spark is the lenient side | Both |
| 9.7 | `STRING_SPLIT('a.b.c', '.')` | `split` takes a Java regular expression, so `'.'` matches every character, and it returns an array ([Spark string functions][s-fstring], [Databricks `split`][d-split]) | `split` with the separator escaped as a regex literal, then `explode` or `posexplode` | Both |
| 9.8 | `STUFF('abc', 0, 1, 'x')` | `overlay` is the nearest function; its out-of-range behaviour is not documented ([Spark string functions][s-fstring]) | Guard the arguments explicitly | Inferred |
| 9.9 | `SELECT TOP 10 * FROM t` without `ORDER BY` | `LIMIT` is "used in conjunction with ORDER BY to ensure that the results are deterministic"; without it the rows returned are not defined ([Spark LIMIT][s-limit], [Databricks LIMIT][d-limit]) | None: no ordering exists to reproduce. The two engines, and two runs of either, can return different rows | Both |
| 9.10 | `SELECT TOP 5 WITH TIES … ORDER BY x` | `LIMIT` has no ties option ([Spark LIMIT][s-limit]) | A `rank()` window filtered to `<= 5`. Dropping `WITH TIES` silently loses rows | Inferred |
| 9.11 | `SELECT … INTO #tmp FROM src`, then `src` is modified, then `#tmp` is read | A DataFrame or temp view is lazily evaluated and recomputed when used, so it sees the modified `src` and re-evaluates non-deterministic expressions ([PySpark quickstart][p-quick], [createOrReplaceTempView][p-tempview]) | Write the intermediate result to a table. Whether `cache()` gives a guaranteed snapshot is not established (§11) | Both |
| 9.12 | `UPDATE t SET x = RAND()` | `rand()` produces an independent value per row ([Spark math functions][s-fmath], [PySpark `rand`][p-rand]) | Generate one value in Python and pass it as a literal | Both |
| 9.13 | `SELECT @v = col FROM t` | `DataFrame.first()` returns the first row, or `None` when there are none ([PySpark `first`][p-first]) | None. Emit `row = df.first()` and assign only when `row` is not `None`; several rows need an explicit ordering | Both |
| 9.14 | `'C:\temp\new'` inside embedded SQL text (the escape hatch, `expr`, `selectExpr`) | `\` escapes in SQL string literals, so `\t` and `\n` become a tab and a newline ([Spark literals][s-literals]) | A raw literal `r'…'`, or `spark.sql.parser.escapedStringLiterals=true` ([Spark literals][s-literals]) | Inferred |

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
