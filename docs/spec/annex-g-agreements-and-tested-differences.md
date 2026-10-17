# Annex G. Silent differences: what agrees, what was not established, and what was tested

Part of the [v1 architecture spec](v1-architecture-spec.md). Annexes C and D hold the catalogued differences; this annex holds the rest of the catalogue's findings, copied unchanged.

- **Sections 10 and 11** come from [the research catalogue](../research/tsql-spark-silent-differences.md): behaviours checked and found to agree, and what could not be established. Section 11 is the list of open points a builder must confirm by reconciliation before relying on them.
- **The tested inferred differences** come from [`corpus/differences/results.md`](../../corpus/differences/results.md): fourteen entries inferred from documentation were run on SQL Server 2022 and local Spark 4.0.0. Finding 7.6 there is the behaviour the `TRY`/`CATCH` helper must reproduce.

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

## Tested inferred differences

Generated by `python corpus/build.py --differences`. Do not edit by hand.

The research in `docs/research/tsql-spark-silent-differences.md` marks 14 differences as inferred: documented on one side only. Each was run on both sides here. The cases are in `cases.py`; the raw observations are in `results.json`.

- SQL Server 16.0.4295.3 (CU27), Developer Edition (64-bit), database collation `SQL_Latin1_General_CP1_CI_AS`
- Spark 4.0.0 with Delta 4.0.0, ANSI mode on. local, open-source Spark with Delta Lake; not a Databricks Runtime.

Result: 13 confirmed, 1 refuted.

| Id | Difference | Verdict | What was observed |
|---|---|---|---|
| 2.2 | String keys differing only in case, in JOIN, GROUP BY, DISTINCT, IN and set operators | confirmed | T-SQL treats the three spellings as one key in every operator; Spark treats them as three. |
| 3.11 | money / money | confirmed | money / money keeps 4 decimal places (3.3333); DECIMAL(19, 4) division keeps 19 (3.3333333333333333333). T-SQL truncates the quotient (2 / 3 = 0.6666), it does not round. |
| 3.12 | ISNUMERIC('$') | confirmed | ISNUMERIC is 1 for '$', '+' and '-'; Spark has no ISNUMERIC and try_cast says none of them is a number. |
| 6.4 | SCOPE_IDENTITY(), @@IDENTITY, IDENT_CURRENT | confirmed | All three return the last identity value in T-SQL; none of them exists in Spark. Open-source Delta 4.0 also rejects identity columns in SQL DDL. |
| 7.2 | Three inserts, the second violating a constraint, XACT_ABORT OFF, no TRY | confirmed | T-SQL reports the failed insert and still runs the third; the Python script stops at the second. |
| 7.6 | What BEGIN TRY ... BEGIN CATCH catches | confirmed | In T-SQL, CATCH does not run for a severity-10 message, and does not run for a missing table referenced at the same level: the procedure stops with an error instead. It does run for divide by zero, and for the same missing table when it is inside a called procedure. A Python except catches the missing table and an unresolved column. A Spark SQL exit handler could not be tried: SQL scripting is off by default in this Spark version. |
| 8.1 | MERGE ... ON t.code = s.code with string keys | confirmed | T-SQL matched 'ab01  ' to 'AB01' and updated it; Spark found no match and inserted a second row. |
| 8.3 | MERGE ... OUTPUT $action, inserted.*, deleted.* | confirmed | T-SQL returns one row per action; Spark rejects the OUTPUT clause as a syntax error. |
| 8.4 | A MERGE target that has triggers | confirmed | Both AFTER triggers fired once for the MERGE in T-SQL; Spark has no CREATE TRIGGER. |
| 8.5 | MERGE with one unconditional WHEN MATCHED THEN DELETE and several source rows per target row | refuted | No difference: both delete the row matched by two source rows and leave the other. |
| 9.4 | SUBSTRING('abcdef', 0, 3) | confirmed | From position 0, T-SQL returns 'ab' and Spark returns 'abc'. From position -1, T-SQL returns 'a' and Spark returns 'f'. |
| 9.8 | STUFF('abc', 0, 1, 'x') | confirmed | STUFF returns NULL for start 0, a start past the end and a negative length. overlay returns 'xabc', 'abcx' and 'axc' for the same three. |
| 9.10 | SELECT TOP 5 WITH TIES ... ORDER BY x | confirmed | TOP 5 WITH TIES returns 7 rows; LIMIT 5 returns 5 and has no WITH TIES form. A rank() window filtered to <= 5 returns the same 7. |
| 9.14 | 'C:\temp\new' inside embedded SQL text | confirmed | T-SQL keeps the backslashes (length 11); Spark turns \t and \n into a tab and a newline (length 9). A raw literal r'...' keeps them. |

## Also checked

Not one of the 14. The research inferred that the two sides agree here; "confirmed" means they do.

| Id | Behaviour | Verdict | What was observed |
|---|---|---|---|
| 10 (empty input) | SUM, AVG, MAX and COUNT(*) over no rows | confirmed | Both return NULL for SUM, AVG and MAX and 0 for COUNT(*) over no rows. |

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
