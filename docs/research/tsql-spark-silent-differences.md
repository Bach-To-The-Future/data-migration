# Where T-SQL and Spark silently disagree

Research for issue #6. It catalogues T-SQL behaviours that give a different result in Spark without raising an error, so that the decision ticket "How is each construct with no PySpark equivalent converted?" can decide, per difference, whether to reproduce T-SQL, accept Spark's behaviour and report it, or mark it manual.

## How this was established

- **Sources.** Only primary documentation: Microsoft's T-SQL reference (SQL Server 2025 view), the Apache Spark 4.2.0 SQL and PySpark documentation (the version `docs/latest` served on 2026-10-13), Databricks documentation (pages dated September 2026) and Delta Lake documentation.
- **Nothing was executed.** No SQL Server or Spark instance was run. Where a result in a table is worked out from a documented rule and is not itself printed in the documentation, the table says "derived".
- **Evidence labels.** *Both* means each side's behaviour is stated in that side's own documentation. *Inferred* means only one side documents it, and the other side's behaviour is deduced or comes from the absence of a feature.
- **ANSI mode.** "ANSI on" and "ANSI off" refer to `spark.sql.ansi.enabled` (`ANSI_MODE` in Databricks SQL). Where a row does not mention the mode, the Spark result is the same in both.
- **"Spark" covers three layers.** Apache Spark, Delta Lake and Databricks. A row names Databricks when the behaviour or the remedy is documented only there.

## Summary: the differences most likely to corrupt results unnoticed

These are ranked by how common the construct is in T-SQL code and how quietly the result changes.

1. **String comparison (§2).** SQL Server's usual collations ignore case, and `=` ignores trailing spaces. Spark compares bytes by default. Joins, `GROUP BY`, `DISTINCT`, `IN` and `MERGE` keys all match fewer rows, so a converted `MERGE` inserts duplicates where T-SQL updated. Spark 4 and Databricks have collations (`UTF8_LCASE`, `UNICODE_CI`, the `RTRIM` modifier) that reproduce most of this, but only if the target columns or the session are given that collation.
2. **Integer division and `AVG` over integers (§3.1, §3.2).** `5 / 2` is `2` in T-SQL and `2.5` in Spark. `AVG` of an `int` column is an `int` in T-SQL and a `DOUBLE` in Spark. The `div` operator reproduces T-SQL.
3. **`CONCAT` with `NULL` (§5.1).** T-SQL's `CONCAT` treats `NULL` as an empty string. Spark's `concat` returns `NULL`. `concat_ws('', …)` reproduces T-SQL.
4. **`LEN` (§9.1).** T-SQL excludes trailing spaces. Spark's `length` includes them.
5. **`DATEDIFF` (§4.7, §4.8).** T-SQL counts boundaries crossed. Spark's unit form counts whole elapsed units, and Spark's two-argument form takes the end date first. `DATEPART(week …)` and `DATEPART(weekday …)` also differ (§4.9, §4.10).
6. **ANSI mode off (§1).** Overflow, division by zero and failed casts raise errors in T-SQL. With ANSI mode off Spark returns wrapped values or `NULL` and continues, and a converted `CATCH` block never runs. ANSI mode is on by default only from Spark 4.0 and Databricks Runtime 17.0.
7. **Transactions and constraints (§7).** A T-SQL `ROLLBACK` undoes every statement in the transaction. In Spark each write commits on its own unless Databricks transactions are used, which exist only as SQL. Primary key and unique constraints are not enforced on Databricks, so inserts that failed in SQL Server succeed.
8. **`datetime` rounding, precision and time zone (§4.1 to §4.6).** `datetime` rounds to 1/300 of a second, `smalldatetime` to the minute, `datetime2(7)` has one more digit than a Spark timestamp, and `GETDATE()` uses the server's local time while Databricks SQL sessions default to UTC.
9. **Lazy evaluation and non-determinism (§9.9, §9.11 to §9.13).** A temp table is a snapshot and a DataFrame is recomputed on each use. `TOP` without `ORDER BY`, `RAND()` and `SELECT @v = col` each behave differently.
10. **Pattern and position edge cases (§2.8 to §2.10, §9.3 to §9.7).** `LIKE '[a-c]%'`, `SUBSTRING` with a start below 1, `CHARINDEX` with a start of 0 and `STRING_SPLIT` on a regex metacharacter all run in Spark and return something else.

Counts: 82 catalogued differences, 68 documented on both sides and 14 inferred. A further 11 behaviours were checked and found to agree (§10). Section 11 lists what could not be established.

## 1. ANSI mode

ANSI mode decides whether Spark raises an error or silently returns a value. T-SQL raises an error in rows 1.1 to 1.5 under its default settings, so **ANSI on is the closer match**: ANSI off turns each of those errors into a silent difference. Rows 1.6 and 1.7 differ from T-SQL in both modes.

Defaults: `spark.sql.ansi.enabled` is `true` by default from Spark 4.0 ([Spark ANSI][s-ansi], [Spark migration guide][s-migration]) and from Databricks Runtime 17.0 ([Databricks ANSI compliance][d-ansi]). In Databricks SQL, `ANSI_MODE` defaults to `TRUE` for accounts created on or after 19 October 2022 ([ANSI_MODE][d-ansimode]). Earlier runtimes default to off.

| ID | Example | T-SQL | Spark, ANSI on | Spark, ANSI off | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|---|---|
| 1.1 | `SELECT 2147483647 + 1` | Arithmetic overflow error; the query ends ([ARITHABORT][m-arithabort]) | `ARITHMETIC_OVERFLOW` error ([Spark ANSI][s-ansi]) | `-2147483648` ([Spark ANSI][s-ansi]) | ANSI on | Both |
| 1.2 | `SELECT 1 / 0` | Divide-by-zero error. `NULL` only if `ANSI_WARNINGS` and `ARITHABORT` are both `OFF` ([ARITHABORT][m-arithabort]) | `DIVIDE_BY_ZERO` error ([ANSI_MODE][d-ansimode]) | `NULL` ([ANSI_MODE][d-ansimode]) | ANSI on. `try_divide` gives the `NULL` variant in both modes ([Spark ANSI][s-ansi]) | Both |
| 1.3 | `CAST('a' AS int)` | Error ([CAST and CONVERT][m-cast]) | `CAST_INVALID_INPUT` error ([Spark ANSI][s-ansi]) | `NULL` ([Spark ANSI][s-ansi]) | ANSI on. T-SQL `TRY_CAST` ([TRY_CAST][m-trycast]) maps to `try_cast` in both modes | Both |
| 1.4 | `CAST(12345 AS tinyint)` | Overflow error ([ARITHABORT][m-arithabort]) | `CAST_OVERFLOW` error ([ANSI_MODE][d-ansimode]) | `57` ([ANSI_MODE][d-ansimode]) | ANSI on | Both |
| 1.5 | `10 = '10.1'` (integer against string) | The string is converted to `int` because `int` has the higher precedence, and a string that is not an integer fails ([data type precedence][m-precedence], [data type conversion][m-conversion]) | The string is promoted to `BIGINT`; runtime error ([ANSI_MODE][d-ansimode]) | `true` ([ANSI_MODE][d-ansimode]) | ANSI on | Both |
| 1.6 | `'5' + '7.6'` (two strings) | `'57.6'`: `+` concatenates strings ([string concatenation][m-stringconcat]) | Type-mismatch error ([ANSI_MODE][d-ansimode]) | `12.6`: both strings are cast to `DOUBLE` ([ANSI_MODE][d-ansimode]) | `concat`, chosen by the converter from the operand types. No setting | Both |
| 1.7 | `SELECT 5 UNION ALL SELECT '6'` (also `CASE` and `COALESCE` with mixed branches) | `int` column: the lower-precedence string is converted ([data type precedence][m-precedence], [COALESCE][m-coalesce]) | `BIGINT` column ([ANSI_MODE][d-ansimode]) | `STRING` column, so later sorting and comparison are lexical ([ANSI_MODE][d-ansimode]) | Explicit cast of each branch | Both |
| 1.8 | A statement in `BEGIN TRY` that overflows, divides by zero or fails a cast | Control passes to `CATCH` ([TRY...CATCH][m-trycatch]) | An exception is raised, so a converted handler runs | No exception; the handler never runs and the `NULL` or wrapped value flows on (rows 1.1 to 1.4) | ANSI on | Both |

`to_date`, `to_timestamp`, `make_date` and similar functions follow the same split: error with ANSI on, `NULL` with ANSI off ([ANSI_MODE][d-ansimode]).

## 2. Collation, case sensitivity and trailing spaces

SQL Server's behaviour depends on the collation of the instance, database, column or expression. The default collation for the English (United States) locale is `SQL_Latin1_General_CP1_CI_AS`, which is case-insensitive and accent-sensitive ([collation and Unicode support][m-collation]). Spark's default collation is `UTF8_BINARY`, which compares the UTF-8 bytes ([Databricks collation][d-collation], [Spark `collation` function][s-fstring]).

| ID | Example | T-SQL (default collation) | Spark (default collation) | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|---|
| 2.1 | `'abc' = 'ABC'` | True under a `_CI` collation ([collation][m-collation]) | False: `'A' <> 'a'` ([Databricks collation][d-collation]) | `COLLATE UTF8_LCASE` or `UNICODE_CI`, the `collate` function, or a default collation on the table, schema or session (Databricks SQL, Runtime 16.1+) ([Databricks collation][d-collation]) | Both |
| 2.2 | The same values as keys in `JOIN`, `GROUP BY`, `DISTINCT`, `IN`, set operators and `MERGE … ON` | Values differing only in case match or fall in one group. Neither page names these operators; this follows from the comparison rule | They do not match and form separate groups | As 2.1, applied to the key columns | Inferred |
| 2.3 | `'Cafe' = 'Café'` | False under `_AS`, true under `_AI` ([collation][m-collation]) | False. `UTF8_LCASE` does not fold accents ([Databricks collation][d-collation]) | `UNICODE_CI_AI` for accent-insensitive sources ([Databricks collation][d-collation]) | Both |
| 2.4 | `'abc' = 'abc '` | True: strings are padded to equal length before comparison, whatever `ANSI_PADDING` is ([string comparison][m-strcmp]) | False ([Databricks collation][d-collation]) | The `RTRIM` modifier, for example `UTF8_BINARY_RTRIM` or `UTF8_LCASE_RTRIM` (Databricks SQL, Runtime 16.2+) ([Databricks collation][d-collation]); otherwise `rtrim` on both sides | Both |
| 2.5 | `'' = '  '`, so `WHERE col = ''` | True, by the padding rule in 2.4 ([string comparison][m-strcmp]) | False | As 2.4 | Both |
| 2.6 | `'abc ' LIKE 'abc'` on `varchar` | True: trailing blanks in the matched expression are ignored for non-Unicode data, but are significant for `nvarchar` ([LIKE][m-like]) | False: `LIKE` is always sensitive to trailing spaces, even under an `RTRIM` collation ([Databricks `like`][d-like]) | None. Apply `rtrim` to the matched expression when the source column is `char` or `varchar` | Both |
| 2.7 | `ORDER BY`, `MIN`, `MAX`, `<`, `BETWEEN` on strings such as `'apple'` and `'Banana'` | Collation order; with a `_CI` collation `'apple'` sorts before `'Banana'` (derived) ([collation][m-collation]) | Byte order: `'Z' < 'a'`, so `'Banana'` sorts first ([Databricks collation][d-collation]) | `UTF8_LCASE` or `UNICODE_CI` ([Databricks collation][d-collation]). Exact equality with a given SQL Server sort order is not established (§11) | Both |
| 2.8 | `'ABC' LIKE 'a%'` | True under a `_CI` collation ([LIKE][m-like], [collation][m-collation]) | False | `ILIKE`, or `LIKE` on a `UTF8_LCASE` string. `LIKE` supports only `UTF8_BINARY` and `UTF8_LCASE`, with or without `RTRIM` ([Databricks `like`][d-like], [Databricks collation][d-collation]) | Both |
| 2.9 | `'apple' LIKE '[a-c]%'` | True: `[ ]` and `[^ ]` are wildcards ([LIKE][m-like]) | False: only `%` and `_` are special, every other character is literal ([Spark LIKE][s-like], [Databricks `like`][d-like]) | `RLIKE` with the pattern rewritten as a regular expression | Both |
| 2.10 | `col LIKE 'a\_b'` | No default escape character, so `\` is literal and `_` matches any character ([LIKE][m-like]) | `\` is the default escape character, so `\_` matches only a literal underscore ([Spark LIKE][s-like]) | An explicit `ESCAPE` clause naming a character the pattern does not use | Both |
| 2.11 | `REPLACE('ABCabc', 'abc', 'x')`; also `CHARINDEX` | `'xx'` under a `_CI` collation (derived): both functions compare using the input collation ([REPLACE][m-replace], [CHARINDEX][m-charindex]) | `'ABCx'`: the documented example `replace('ABCabc', 'abc', 'DEF')` returns `'ABCDEF'` ([Spark string functions][s-fstring]) | Not established whether these functions honour a non-binary collation (§11) | Both |
| 2.12 | `CAST('ab' AS char(5)) + '!'` | `'ab   !'` (derived): `char(n)` is fixed-size and padded with blanks ([char and varchar][m-charvarchar], [ANSI_PADDING][m-ansipadding]) | `'ab!'`: `CAST` treats `CHAR` and `VARCHAR` as `STRING`; only table columns pad ([Spark migration guide][s-migration], [Spark data types][s-types]) | `rpad(x, n)` | Both |
| 2.13 | `CAST('abcdef' AS varchar(3))`; `CAST(x AS varchar)` (length 30); `DECLARE @v varchar` (length 1) | Silently truncated to the length ([char and varchar][m-charvarchar], [CAST and CONVERT][m-cast]) | Not truncated: `CAST` ignores the length ([Spark migration guide][s-migration]) | `substring(x, 1, n)` or `left(x, n)` | Both |

Identifiers are case-insensitive in Spark and Databricks ([Spark identifiers][s-ident], [Databricks identifiers][d-ident]), which matches SQL Server under a case-insensitive collation.

## 3. Numbers: division, implicit conversion, decimal precision

| ID | Example | T-SQL | Spark | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|---|
| 3.1 | `5 / 2` on integers | `2`: the fractional part is truncated ([division][m-divide]) | `2.5`: `/` returns `DOUBLE` unless both operands are `DECIMAL` ([Databricks `/`][d-slash]) | `div`, which returns the integral part as `BIGINT` and truncates toward zero (`-5.9 div 1` is `-5`) ([Databricks `div`][d-div], [Spark math functions][s-fmath]) | Both |
| 3.2 | `AVG(c)` over an `int` column holding 1 and 2 | `1` (derived): the return type is `int` ([AVG][m-avg]) | `1.5`: the result is `DOUBLE` ([Databricks `avg`][d-avg]) | `sum(c) div count(c)` | Both |
| 3.3 | `AVG(c)` over `decimal(p, s)` | `decimal(38, max(s, 6))` ([AVG][m-avg]) | `DECIMAL(p + 4, s + 4)` ([Databricks `avg`][d-avg]) | Cast the result to the T-SQL type. Whether the last digit then agrees is not established (§11) | Both |
| 3.4 | `SUM(c)` | `int` for `int` input, so it fails above the `int` range; `decimal(38, s)` for decimal input ([SUM][m-sum]) | `BIGINT` for any integral input; `DECIMAL(p + min(10, 31-p), s)` for decimal input. ANSI off: a `BIGINT` overflow wraps ([Databricks `sum`][d-sum]) | Cast the result to the T-SQL type | Both |
| 3.5 | `POWER(2, -100.0)` | `0`: the return type follows the first argument, here `int` ([POWER][m-power]) | About `7.9E-31`: `pow` returns `DOUBLE` ([Databricks `pow`][d-pow]) | Cast the result to the first argument's type | Both |
| 3.6 | `ROUND(123.456, 2, 1)` | `123.450` (derived): a non-zero third argument truncates, and the return type keeps the input's precision and scale ([ROUND][m-round]) | `round` takes two arguments, always rounds half up and reduces the scale ([Databricks `round`][d-round]) | None found. A converter that drops the third argument rounds where T-SQL truncated | Both |
| 3.7 | `tinyint` value `200` | Valid: the range is 0 to 255 ([integer types][m-int]) | `TINYINT` is -128 to 127 ([Databricks TINYINT][d-tinyint]). ANSI on: overflow error. ANSI off: wraps | Map `tinyint` to `SMALLINT` | Both |
| 3.8 | `CAST(12345678901 AS decimal)` | Fits: the default is `decimal(18, 0)` ([decimal and numeric][m-decimal]) | Default is `DECIMAL(10, 0)` ([Databricks DECIMAL][d-decimal]). ANSI on: overflow error. ANSI off: no error | Always emit an explicit precision and scale | Both |
| 3.9 | `CAST('' AS int)` | `0` ([char and varchar][m-charvarchar]) | ANSI on: `CAST_INVALID_INPUT`. ANSI off: `NULL` ([Databricks `cast`][d-cast]) | None. Emit an explicit test for the empty string | Both |
| 3.10 | `CAST(CAST(1234567.891 AS float) AS varchar(30))` | At most 6 digits, in scientific notation when needed (style 0) ([CAST and CONVERT][m-cast]) | Plain notation, without an exponent, for magnitudes from 0.001 up to 10,000,000 ([Databricks `cast`][d-cast]) | None. Format explicitly | Both |
| 3.11 | `money` arithmetic, for example `money / money` | `money` keeps four decimal places and Microsoft warns of rounding through truncation ([money][m-money]) | Spark has no `money` type; as `DECIMAL(19, 4)` a division follows the decimal scale rules and keeps more digits ([Spark ANSI][s-ansi]) | Cast each intermediate result to `DECIMAL(19, 4)` | Inferred |
| 3.12 | `ISNUMERIC('$')` | `1`; also `1` for `+` and `-` ([ISNUMERIC][m-isnumeric]) | No equivalent function. An emulation such as `try_cast(x AS DOUBLE) IS NOT NULL` gives a different answer for these inputs | None | Inferred |

Decimal arithmetic itself is covered in §10: the result precision and scale formulas are the same on both sides.

## 4. Dates and times

| ID | Example | T-SQL | Spark | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|---|
| 4.1 | `CAST('2024-01-01 23:59:59.999' AS datetime)` | `2024-01-02 00:00:00.000`: `datetime` rounds to increments of .000, .003 or .007 seconds ([datetime][m-datetime]) | The timestamp keeps `.999`: six fractional digits are stored and nothing is rounded to a coarser grid ([Databricks TIMESTAMP][d-timestamp]) | None. Matters for end-of-day predicates and for comparing against stored `datetime` values | Both |
| 4.2 | `CAST('2024-05-09 23:59:59' AS smalldatetime)` | `2024-05-10 00:00:00`: rounds to the nearest minute ([smalldatetime][m-smalldatetime]) | The timestamp keeps the seconds ([Databricks TIMESTAMP][d-timestamp]) | None. Emulate by adding 30 seconds and truncating to the minute | Both |
| 4.3 | `datetime2(7)`, `time(7)`, `datetimeoffset(7)`, `SYSDATETIME()` | Seven fractional digits, 100 ns ([datetime2][m-datetime2], [SYSDATETIME][m-sysdatetime]) | Six fractional digits by default; extra digits in a string or cast are truncated. Databricks `TIMESTAMP(p)` with 7 to 9 digits is in Beta ([Databricks TIMESTAMP][d-timestamp], [Databricks `cast`][d-cast]) | None outside the Beta type | Both |
| 4.4 | `datetimeoffset` value `2024-05-08 12:35:29 +12:15` | The offset is preserved for retrieval; comparison is in UTC ([datetimeoffset][m-datetimeoffset]) | `TIMESTAMP` is an absolute instant shown in the session time zone; the offset is not kept ([Spark data types][s-types]) | None. Carry the offset in a separate column | Both |
| 4.5 | `datetime` or `datetime2` mapped to Spark `TIMESTAMP` | No time zone ([datetime2][m-datetime2]) | `TIMESTAMP` is interpreted in the session time zone, so the same stored value reads differently when the session zone changes; `TIMESTAMP_NTZ` ignores time zones ([Spark data types][s-types]) | `TIMESTAMP_NTZ` | Both |
| 4.6 | `GETDATE()`, `CAST(GETDATE() AS date)` | The local time of the server's operating system, as `datetime` ([GETDATE][m-getdate]) | `current_timestamp()` in the session time zone, which defaults to UTC in Databricks SQL and to the JVM's zone in Apache Spark ([Databricks TIMEZONE][d-timezone], [Spark configuration][s-config]) | Set the session time zone to the source server's zone | Both |
| 4.7 | `DATEDIFF(year, '2005-12-31 23:59:59.9999999', '2006-01-01 00:00:00.0000000')` | `1`: counts datepart boundaries crossed ([DATEDIFF][m-datediff]) | `0` (derived): `datediff(unit, start, end)` and `timestamp_diff` count whole elapsed units ([Databricks `datediff` (timestamp)][d-datediff3], [PySpark `timestamp_diff`][p-tsdiff]) | None single. Year: `year(end) - year(start)`. Month: year difference times 12 plus month difference. Day: two-argument `datediff` on dates. Smaller units: truncate both sides with `date_trunc`, then difference | Both |
| 4.8 | `DATEDIFF(day, @start, @end)` | Start first, end second ([DATEDIFF][m-datediff]) | Two-argument `datediff(endDate, startDate)` takes the end first ([Spark datetime functions][s-fdatetime], [PySpark `datediff`][p-datediff]) | Swap the arguments; keeping T-SQL's order flips the sign | Both |
| 4.9 | `DATEPART(weekday, '2007-04-21')` (a Saturday) | Depends on `SET DATEFIRST`: `7` under the US English default of 7, `6` under `DATEFIRST 1` ([DATEPART][m-datepart], [DATEFIRST][m-datefirst]) | `dayofweek` is fixed at 1 = Sunday, giving `7`; `weekday` is 0 = Monday ([Spark datetime functions][s-fdatetime]) | `dayofweek` only when the source runs with `DATEFIRST 7`; otherwise compute from it | Both |
| 4.10 | `DATEPART(week, '2021-01-01')` | `1` (derived): 1 January always starts week 1, and the week start follows `DATEFIRST` ([DATEPART][m-datepart]) | `53` (derived): `weekofyear` is ISO 8601, Monday start, week 1 has more than 3 days ([Spark datetime functions][s-fdatetime], [Databricks `weekofyear`][d-weekofyear]) | None for `week`. T-SQL's `iso_week` datepart does match `weekofyear` | Both |
| 4.11 | `CAST(0 AS datetime)`, `DATEADD(day, DATEDIFF(day, 0, @d), 0)`, `CAST(@d AS int)` | `0` is 1 January 1900 and integers count days ([DATEPART][m-datepart], [CAST and CONVERT][m-cast]) | A number cast to `TIMESTAMP` is seconds since 1970-01-01 UTC, and the reverse cast gives seconds; both are allowed with ANSI on ([Databricks `cast`][d-cast], [Spark ANSI][s-ansi]) | None. Rewrite the idiom (`date_trunc('DAY', d)` for the common one) | Both |
| 4.12 | `CAST('' AS datetime)` | `1900-01-01` ([char and varchar][m-charvarchar]) | ANSI on: error. ANSI off: `NULL` ([Databricks `cast`][d-cast]) | None | Both |
| 4.13 | `CAST('01/02/2024' AS date)` | Depends on `SET DATEFORMAT` and `SET LANGUAGE`; such conversions are nondeterministic ([DATEPART][m-datepart], [CAST and CONVERT][m-cast]) | A cast accepts only the documented `yyyy-mm-dd` forms. ANSI on: error. ANSI off: `NULL` ([Databricks TIMESTAMP][d-timestamp], [Databricks `cast`][d-cast]) | `to_date` with an explicit pattern chosen from the source's `DATEFORMAT` | Both |

`datetime + 1` adds one day in T-SQL ([+ operator][m-add]); Spark raises a type error for a timestamp plus a number ([Databricks `+`][d-plus]), so that one is not silent.

## 5. NULL handling

| ID | Example | T-SQL | Spark | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|---|
| 5.1 | `CONCAT('a', NULL, 'b')` | `'ab'`: `CONCAT` converts `NULL` to an empty string, and all-`NULL` input gives `''` ([CONCAT][m-concat]) | `NULL`: `concat` is null-intolerant ([Spark NULL semantics][s-null]) | `concat_ws('', …)`, which skips `NULL` ([Spark string functions][s-fstring]) | Both |
| 5.2 | `'abc' + NULL` | `NULL`. Under the deprecated `CONCAT_NULL_YIELDS_NULL OFF` it is `'abc'`; the setting is always `ON` from SQL Server 2017 ([CONCAT_NULL_YIELDS_NULL][m-concatnull]) | `concat` and the double-pipe operator give `NULL` ([Spark NULL semantics][s-null]) | `concat` matches the default. `concat_ws('', …)` matches the legacy `OFF` setting | Both |
| 5.3 | `WHERE col = NULL` with `SET ANSI_NULLS OFF` | Returns the rows where `col` is `NULL`; `<> NULL` returns the non-null rows. With `ON` both return no rows ([ANSI_NULLS][m-ansinulls]) | `=` with a `NULL` operand is always `NULL`, so no rows ([Spark NULL semantics][s-null]) | `<=>` or `Column.eqNullSafe`, or `isNull` ([Spark NULL semantics][s-null], [PySpark `eqNullSafe`][p-eqnullsafe]) | Both |
| 5.4 | `ISNULL(CAST(NULL AS varchar(2)), 'abcdef')` | `'ab'`: the result has the type of the first argument and the replacement can be truncated ([ISNULL][m-isnull]) | `coalesce` returns `'abcdef'`: the result is the least common type ([Spark ANSI][s-ansi]) | Cast the replacement to the first argument's type, with `substring` for strings | Both |
| 5.5 | `ISNULL(int_col, 1.5)` | `1` (derived): the replacement is converted to `int`, which truncates ([ISNULL][m-isnull], [CAST and CONVERT][m-cast]) | `coalesce` returns a decimal `1.5` ([Databricks type rules][d-typerules]) | As 5.4 | Both |
| 5.6 | `COALESCE(varchar_col, 0)` | `int`, by data type precedence ([COALESCE][m-coalesce]) | ANSI on: `BIGINT`. ANSI off: `STRING` (row 1.7) | Explicit casts | Both |

NULL ordering, NULL grouping and aggregates over empty input agree; see §10.

## 6. Row counts and identity

| ID | Example | T-SQL | Spark | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|---|
| 6.1 | `UPDATE …; IF @@ROWCOUNT = 0 …` | `@@ROWCOUNT` is set by almost every statement: rows affected for DML, rows read for `SELECT`, `1` for `SET @v`, `0` after `PRINT`, `USE` or `BEGIN TRANSACTION` ([@@ROWCOUNT][m-rowcount]) | The DataFrame API has no equivalent. Databricks has `GET DIAGNOSTICS … ROW_COUNT` for the latest DML statement, only inside a SQL compound statement (Runtime 18 LTS+) ([GET DIAGNOSTICS][d-getdiag]). Delta records per-operation counts in the table history ([Delta utility][dl-utility], [Databricks history][d-history]) | None in the DataFrame API. Re-counting with `df.count()` runs the query again and can return a different number (row 9.11) | Both |
| 6.2 | `MERGE …; SELECT @@ROWCOUNT` | The total of rows inserted, updated and deleted ([MERGE][m-merge]) | Delta history reports `numTargetRowsInserted`, `numTargetRowsUpdated` and `numTargetRowsDeleted` separately ([Delta utility][dl-utility]) | Sum the three metrics | Both |
| 6.3 | `id int IDENTITY(1, 1)` and code that assumes dense values | Each value is generated from the current seed and increment, on any integer type. Consecutive values are not guaranteed: concurrent inserts, failed or rolled-back statements and restarts leave gaps ([IDENTITY][m-identity]) | Delta identity columns are `BIGINT` only, unique and in multiples of the step, but "not guaranteed to be contiguous" ([Databricks identity columns][d-gencols], [Delta batch][dl-batch]) | None for density. `row_number()` over an explicit ordering gives dense numbers. `monotonically_increasing_id()` is unique but not consecutive and not deterministic ([PySpark][p-mono]) | Both |
| 6.4 | `SCOPE_IDENTITY()`, `@@IDENTITY`, `IDENT_CURRENT` | The last identity value generated in the scope, session or table ([SCOPE_IDENTITY][m-scopeidentity], [@@IDENTITY][m-atidentity]) | No documented equivalent in Spark, Delta or Databricks. Emulating with `max(id)` reads other writers' rows and any explicitly supplied values | None | Inferred |
| 6.5 | `SELECT * INTO new_table FROM t` where `t` has an identity column | The new column inherits the `IDENTITY` property ([SELECT INTO][m-selectinto]) | `CREATE TABLE … AS SELECT` cannot define an identity column, so the copy has plain values ([Databricks identity columns][d-gencols]) | Create the table with the identity column first, then insert | Both |

Declaring a Delta identity column disables concurrent transactions on the table ([Databricks identity columns][d-gencols]). That produces write conflicts, which are errors and not silent.

## 7. Errors and transactions

| ID | Example | T-SQL | Spark | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|---|
| 7.1 | `BEGIN TRAN; INSERT a …; INSERT b …; ROLLBACK` | Every statement in the transaction is undone ([transaction guide][m-txguide]) | By default each statement is its own transaction against one table, and nothing spans statements, so a converted "rollback" leaves the earlier writes committed ([Databricks ACID][d-acid]) | Databricks only: `BEGIN ATOMIC … END` or `BEGIN TRANSACTION … COMMIT` through SQL, on Runtime 18.0+ with Unity Catalog managed tables that have catalog commits enabled; DML and `SELECT` only, no DDL ([Databricks transactions][d-txn]). Apache Spark with Delta: none ([Delta concurrency][dl-cc]) | Both |
| 7.2 | Three inserts, the second violating a constraint, `XACT_ABORT OFF`, no `TRY` | Only the failing statement is rolled back; the batch continues and the third insert runs ([XACT_ABORT][m-xactabort], [transaction guide][m-txguide]) | An uncaught Python exception stops the script at the second statement, so the third never runs. This is Python's behaviour and is not stated in the Spark documentation | A SQL compound statement with a `CONTINUE HANDLER`, which resumes at the next statement ([Spark SQL scripting][s-scripting], [compound statement][d-compound]); in Python, a `try` around each statement | Inferred |
| 7.3 | The same with `SET XACT_ABORT ON` | The whole transaction is rolled back and the batch ends ([XACT_ABORT][m-xactabort]) | `BEGIN ATOMIC` rolls back all changes when any statement fails ([compound statement][d-compound]) | `BEGIN ATOMIC`, within the limits in 7.1. Handlers are not allowed inside an atomic block | Both |
| 7.4 | An explicit transaction with `XACT_ABORT OFF` where one statement fails and the code then commits | The failing statement is rolled back and the transaction can continue and commit the rest ([XACT_ABORT][m-xactabort]) | An atomic block rolls back everything. For an interactive transaction the documentation says only that you must run `ROLLBACK` ([Databricks transactions][d-txn]) | None found | Both |
| 7.5 | `INSERT` of a duplicate key into a table with a primary key or unique constraint, often inside `TRY…CATCH` as an upsert | The insert fails and the error is caught ([TRY...CATCH][m-trycatch]) | Primary key, foreign key and unique constraints "are informational only and aren't enforced", so the duplicate is inserted ([Databricks constraints][d-constraints]) | None. Rewrite as `MERGE`, or check before inserting | Both |
| 7.6 | `BEGIN TRY … END TRY BEGIN CATCH … END CATCH` | Catches errors of severity above 10 that do not close the connection. Does not catch warnings, or compile errors at the same execution level ([TRY...CATCH][m-trycatch]) | A Python `except` catches every exception, including analysis errors such as an unresolved column; a SQL `SQLEXCEPTION` handler covers every SQLSTATE outside classes `XX` and `02` ([Spark SQL scripting][s-scripting]) | None exact. The handler can run for errors T-SQL would not have routed to `CATCH` | Inferred |

Row 1.8 is the other half of 7.6: with ANSI off, errors that T-SQL would have caught never occur.

## 8. MERGE

| ID | Example | T-SQL | Spark (Delta) | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|---|
| 8.1 | `MERGE … ON t.code = s.code` with string keys | Keys match ignoring case and trailing spaces (rows 2.1, 2.4), so the row is updated | The keys do not match under `UTF8_BINARY`, so `WHEN NOT MATCHED` inserts a second row | Collation on the key columns, as in §2 | Inferred |
| 8.2 | `UPDATE t SET … FROM t JOIN s …` where several `s` rows match one `t` row | No error; which source row is used is undefined ([UPDATE][m-update]) | The documented `UPDATE` syntax has no `FROM` clause ([Databricks UPDATE][d-update]); the natural rewrite as `MERGE` fails with `DELTA_MULTIPLE_SOURCE_ROW_MATCHING_TARGET_ROW_IN_MERGE` ([Databricks MERGE][d-merge]) | None. Deduplicate the source with an explicit rule. This is an error and not silent, but the source code may depend on the lenient behaviour | Both |
| 8.3 | `MERGE … OUTPUT $action, inserted.*, deleted.*` | Returns one row for every row inserted, updated or deleted ([MERGE][m-merge], [OUTPUT][m-output]) | The documented Delta `MERGE` syntax has no `OUTPUT` or returning clause ([Databricks MERGE][d-merge]). Reading the table before and after is not atomic with the merge | None found (§11) | Inferred |
| 8.4 | A `MERGE` target that has triggers | `AFTER` and `INSTEAD OF` triggers fire for each action ([MERGE][m-merge]) | The documented Delta DML has no triggers, so their side effects do not happen | None | Inferred |
| 8.5 | `MERGE` with one unconditional `WHEN MATCHED THEN DELETE` and several source rows per target row | `MERGE` cannot update the same row more than once, or update and delete the same row; the page does not state the delete-only case ([MERGE][m-merge]) | Allowed: "an unconditional delete is not ambiguous" ([Databricks MERGE][d-merge]) | Not needed; Spark succeeds with the intended result | Inferred |

Two `MERGE` behaviours on the ticket's list agree and are in §10: several source rows matching one target row under an `UPDATE` action, and `WHEN NOT MATCHED BY SOURCE`.

## 9. String functions, TOP and other statements

| ID | Example | T-SQL | Spark | Built-in that reproduces T-SQL | Evidence |
|---|---|---|---|---|---|
| 9.1 | `LEN('ab  ')` | `2`: trailing spaces are excluded ([LEN][m-len]) | `length` returns `4`: "includes the trailing spaces" ([Spark string functions][s-fstring]) | `length(rtrim(x))` | Both |
| 9.2 | `DATALENGTH(N'abc')` | `6` (derived): bytes, and `nvarchar` uses byte-pairs ([DATALENGTH][m-datalength], [precision, scale and length][m-precision]) | `octet_length('abc')` is `3` (derived): the byte length of the string, which Databricks stores as UTF-8 ([Spark string functions][s-fstring], [Databricks collation][d-collation]) | None. Use `length(x) * 2` for `nvarchar` without supplementary characters | Both |
| 9.3 | `SUBSTRING('abcdef', -1, 3)` | `'a'` (derived): a start below 1 begins at the first character and returns `start + length - 1` characters ([SUBSTRING][m-substring]) | `'f'` (derived): a negative position counts from the end ([Spark string functions][s-fstring], [Databricks `substr`][d-substr]) | None. When the start can be below 1, emit `substring(x, 1, greatest(start + len - 1, 0))` | Both |
| 9.4 | `SUBSTRING('abcdef', 0, 3)` | `'ab'` (derived, same rule) ([SUBSTRING][m-substring]) | Not documented for position 0; PySpark says only that the position is 1-based ([PySpark `substring`][p-substring]) | As 9.3 | Inferred |
| 9.5 | `CHARINDEX('b', 'abc', 0)` | `2`: a start of zero or below searches from the beginning ([CHARINDEX][m-charindex]) | `0`: "if pos is less than 1, the result is 0" ([Databricks `charindex`][d-charindex]) | `charindex` or `locate` with `greatest(start, 1)`. Note `instr(str, substr)` takes its arguments in the opposite order ([Spark string functions][s-fstring]) | Both |
| 9.6 | `LEFT('abc', -1)`, `RIGHT('abc', -1)` | Error ([LEFT][m-left]) | Empty string ([Spark string functions][s-fstring]) | None; Spark is the lenient side | Both |
| 9.7 | `STRING_SPLIT('a.b.c', '.')` | Rows `a`, `b`, `c`, in no guaranteed order unless the ordinal column is requested ([STRING_SPLIT][m-stringsplit]) | `split` takes a Java regular expression, so `'.'` matches every character, and it returns an array ([Spark string functions][s-fstring], [Databricks `split`][d-split]) | `split` with the separator escaped as a regex literal, then `explode` or `posexplode` | Both |
| 9.8 | `STUFF('abc', 0, 1, 'x')` | `NULL` when the start is zero, negative or past the end, or the length is negative ([STUFF][m-stuff]) | `overlay` is the nearest function; its out-of-range behaviour is not documented ([Spark string functions][s-fstring]) | Guard the arguments explicitly | Inferred |
| 9.9 | `SELECT TOP 10 * FROM t` without `ORDER BY` | Ten rows "in an undefined order" ([TOP][m-top]) | `LIMIT` is "used in conjunction with ORDER BY to ensure that the results are deterministic"; without it the rows returned are not defined ([Spark LIMIT][s-limit], [Databricks LIMIT][d-limit]) | None: no ordering exists to reproduce. The two engines, and two runs of either, can return different rows | Both |
| 9.10 | `SELECT TOP 5 WITH TIES … ORDER BY x` | Also returns rows that tie with the last row ([TOP][m-top]) | `LIMIT` has no ties option ([Spark LIMIT][s-limit]) | A `rank()` window filtered to `<= 5`. Dropping `WITH TIES` silently loses rows | Inferred |
| 9.11 | `SELECT … INTO #tmp FROM src`, then `src` is modified, then `#tmp` is read | `#tmp` is a table holding the rows as they were ([SELECT INTO][m-selectinto]) | A DataFrame or temp view is lazily evaluated and recomputed when used, so it sees the modified `src` and re-evaluates non-deterministic expressions ([PySpark quickstart][p-quick], [createOrReplaceTempView][p-tempview]) | Write the intermediate result to a table. Whether `cache()` gives a guaranteed snapshot is not established (§11) | Both |
| 9.12 | `UPDATE t SET x = RAND()` | Every affected row gets the same value ([RAND][m-rand]) | `rand()` produces an independent value per row ([Spark math functions][s-fmath], [PySpark `rand`][p-rand]) | Generate one value in Python and pass it as a literal | Both |
| 9.13 | `SELECT @v = col FROM t` | Several rows: `@v` gets the last value returned. No rows: `@v` keeps its previous value ([SELECT @local_variable][m-selectvar]) | `DataFrame.first()` returns the first row, or `None` when there are none ([PySpark `first`][p-first]) | None. Emit `row = df.first()` and assign only when `row` is not `None`; several rows need an explicit ordering | Both |
| 9.14 | `'C:\temp\new'` inside embedded SQL text (the escape hatch, `expr`, `selectExpr`) | Backslash is an ordinary character: the constants page defines no backslash escape sequences ([constants][m-constants]) | `\` escapes in SQL string literals, so `\t` and `\n` become a tab and a newline ([Spark literals][s-literals]) | A raw literal `r'…'`, or `spark.sql.parser.escapedStringLiterals=true` ([Spark literals][s-literals]) | Inferred |

## 10. Checked and found to agree

These were on the ticket's list or adjacent to it. No silent difference was found.

| Behaviour | T-SQL | Spark | Evidence |
|---|---|---|---|
| Ordering of `NULL` | `NULL` is the lowest value: first ascending, last descending ([ORDER BY][m-orderby]) | `NULLS FIRST` ascending, `NULLS LAST` descending by default ([Spark ORDER BY][s-orderby]) | Both |
| `NULL` in `GROUP BY` and `DISTINCT` | Nulls are equal for `DISTINCT` ([SELECT clause][m-distinct]) | Nulls are grouped into one bucket ([Spark NULL semantics][s-null]) | Both |
| Aggregates ignore `NULL`; empty input | `SUM` and `AVG` ignore nulls ([SUM][m-sum], [AVG][m-avg]). The pages read do not state the empty-input result | `count(*)` on empty input is `0`; `max`, `sum` and the like return `NULL` ([Spark NULL semantics][s-null]) | Inferred |
| Decimal result precision and scale for `+ - * / %` | The same five formulas ([precision, scale and length][m-precision]) | The same five formulas, with a minimum scale of 6 kept when precision exceeds 38 ([Spark ANSI][s-ansi]) | Both |
| `ROUND` tie-breaking | Half away from zero ([ROUND][m-round]) | `HALF_UP` ([Databricks `round`][d-round], [PySpark `round`][p-round]) | Both |
| Cast to an integer type | `numeric` and `float` are truncated ([CAST and CONVERT][m-cast]) | Truncated to a whole number ([Databricks `cast`][d-cast]) | Both |
| `DATEADD(month, 1, '2024-08-31')` | The last day of the month when the day does not exist ([DATEADD][m-dateadd]) | `add_months('2016-08-31', 1)` is `2016-09-30` ([Spark datetime functions][s-fdatetime]) | Both |
| `CONCAT_WS` and `STRING_AGG` with `NULL` | Nulls are skipped ([CONCAT_WS][m-concatws], [STRING_AGG][m-stringagg]) | Nulls are skipped ([Spark string functions][s-fstring], [Databricks `string_agg`][d-stringagg]) | Both |
| `TRIM`, `LTRIM`, `RTRIM` default | Remove the space character `char(32)` ([TRIM][m-trim]) | Remove space characters ([Spark string functions][s-fstring]) | Both |
| `MERGE`: several source rows match one target row under `UPDATE` | Error ([MERGE][m-merge]) | Error ([Databricks MERGE][d-merge], [Delta update][dl-update]) | Both |
| `MERGE … WHEN NOT MATCHED BY SOURCE` | Supported; the condition may reference only target columns; at most two such clauses ([MERGE][m-merge]) | Supported with the same restriction and any number of clauses, from Databricks Runtime 12.2 LTS and Delta 2.3 (API) or 2.4 (SQL) ([Databricks MERGE][d-merge], [Delta update][dl-update]) | Both |

## 11. Could not be established

- **Nothing was run.** Every "derived" result should be confirmed by reconciliation before the tool relies on it.
- **Collation equivalence.** Whether any Spark collation orders and compares exactly like a given SQL Server collation such as `SQL_Latin1_General_CP1_CI_AS`. The documentation of each side describes its own rules only.
- **Collation in Spark string functions.** Whether `replace`, `locate`, `instr` and `split` honour a non-binary collation. Databricks documents the limitation for `LIKE` and the regex functions only.
- **Which value represents a case-insensitive group.** For `GROUP BY` or `DISTINCT` over `'abc'` and `'ABC'`, neither side documents which spelling is returned.
- **Spark `substring` at position 0**, **`overlay` out of range** and **`replace` with an empty search string**: not documented on the Spark side.
- **Decimal edge cases.** Whether the two engines agree when a result needs more than 38 digits; what precision Spark gives an `INT` or `BIGINT` operand in decimal arithmetic; whether T-SQL's `AVG` rounds or truncates its last digit; `ROUND` on `float` inputs.
- **Integer literals above the `int` range.** T-SQL makes them `decimal` ([integer types][m-int]). The Spark literals page does not say what an unsuffixed literal of that size becomes.
- **Databricks transactions and the DataFrame API.** The transactions page lists SQL statements only. Whether `DataFrameWriter` or `DeltaTable` operations take part in a transaction, and what a failed statement leaves behind in an interactive transaction, is not stated.
- **A Delta equivalent of `OUTPUT`.** Change Data Feed was not researched.
- **`ANSI_NULLS` as captured by stored procedures.** The page read says the setting applies at run time; how it is stored with a procedure was not checked.
- **Empty-input aggregates on the T-SQL side** (see §10).
- **Whether `DataFrame.cache()` is a reliable snapshot** for row 9.11.

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
