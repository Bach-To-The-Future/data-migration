# What a 2022 sandbox at the client's level does not reproduce from SQL Server 2008 to 2016

Research for [Which behaviours differ between SQL Server 2008–2016 and a newer engine at the same compatibility level?](https://github.com/Bach-To-The-Future/data-migration/issues/33), 17 October 2026.

[ADR 0015](../adr/0015-old-compatibility-levels-reconcile-only-on-a-guarded-test-server.md) routes a database from SQL Server 2008, 2008 R2, 2012, 2014 or 2016, at compatibility level 100 or above, to a SQL Server 2022 container set to the client's level. This catalogue lists what that container does not reproduce, so that the parser can flag an artefact that uses a listed construct and the report can name it. It is used like the [silent-differences catalogue](tsql-spark-silent-differences.md).

## Answer

**The compatibility level covers most language changes, and the container reproduced every level-gated difference that was tested, with one exception.** Twenty-nine documented level-gated behaviours were run on SQL Server 2022 at levels 100 to 160. In twenty-eight, the result below the documented level was the old behaviour and the result at or above it was the new one (§3). One more documented row, on hash algorithms, could not be matched to any statement (§3.17).

**What a level does not cover falls into three groups.**

- **Removed features (§1).** They fail loudly at every level: `COMPUTE`, the old `RAISERROR` form, `FASTFIRSTROW`, `sp_dboption` and fifteen more. An artefact that uses one cannot be run in the sandbox at all, so it must be reported as not reconcilable, not as failed.
- **Changes that are not gated (§2).** The same statement gives a different result, or succeeds where it failed. Microsoft documents few of these, and most are about metadata, error text or limits.
- **Plan-dependent results (§2.1 to §2.3).** Constructs whose result depends on the query plan are undefined on any engine, and the newer engine's plans and wrong-result fixes are not frozen by the level.

**The exception is RC4 encryption (§2.4).** Microsoft's current documentation says new data can still be encrypted with an RC4 key at level 100. On SQL Server 2017, 2019, 2022 and 2025 at level 100 the key is created and opened without error, and `ENCRYPTBYKEY` returns `NULL`. That is silent.

**One documented change turned out to be gated although the documentation does not say so, and two documented removals still work:**

- `xml.exist()` on a `NULL` value compared with 0 returns the old result at level 100 and the new one from level 110 (§3.18).
- `DATABASEPROPERTY` and the version argument of `PWDCOMPARE` are listed as discontinued in SQL Server 2012, but both still run on 2017 to 2025 (§4).

## How this was established

- **Documentation.** The "Discontinued", "Deprecated", "Breaking changes" and "Behavior changes" pages for each version from 2012, the `ALTER DATABASE` compatibility level reference with its tables of differences by level, the SQL Server 2008 R2 compatibility level page, Microsoft's article on data type improvements at level 130 (KB 4010261), and T-SQL reference pages for the functions named in the tables. Current pages were read from Microsoft Learn on 17 October 2026. Pages for 2012, 2014 and 2016 exist only in Microsoft's previous-versions archive.
- **Gaps in the documentation itself.** Microsoft's 2019 page says the release "doesn't introduce breaking changes" ([Breaking changes 2019][brk19]). No breaking-changes page exists for SQL Server 2022. The 2014 page says "No new issues" for 2014. Most documented changes therefore come from SQL Server 2012 and 2016.
- **Tests.** One script of about 75 checks was run in a fresh database at each compatibility level on four Linux container images: SQL Server 2017 CU31 (levels 100 to 140), 2019 CU32 (100 to 150), 2022 CU27 (100 to 160) and 2025 CU9 (100 to 170). Each container was started with a random password and no published port, and removed afterwards. A short follow-up script ran on 2022 at level 100.
- **What a test shows.** A test shows the newer engine's side only. No SQL Server 2008 to 2016 engine was run, so the "old" column always rests on Microsoft's documentation. Where the 2022 result at every level differs from the documented old behaviour, the difference is not gated.
- **Across the newer versions.** For every check, 2017, 2019 and 2025 gave the same result as 2022 at the same level, except: the text of error 321; the `clr strict security` value on 2017 (§1.18); the number of collations; and the database version number that `DATABASEPROPERTY` returns (§2.13).
- **Evidence labels.** *[docs]* means Microsoft documents the behaviour. *[ran]* means it was observed in a container on 17 October 2026. *[inferred]* means it is deduced and neither documented nor observed.
- **Scope.** The sources are 2008 to 2016 databases at level 100 or above. Changes made in SQL Server 2005 or 2008, or seen only at levels 80 and 90, are out of scope: a 2008 database at level 100 already has the new behaviour. Gaps between the container and a Windows server, such as SSIS, unsafe CLR and linked servers, are in [sandbox fidelity](sandbox-fidelity.md) and are not repeated here.

## Summary: the entries most likely to change results silently

These are the entries where the sandbox gives a result, and that result can differ from the client's server without any error.

1. **Plan-dependent results (§2.1, §2.2).** An `ORDER BY` in a `TOP 100 PERCENT` view is ignored on both engines, so the order the client's reports rely on is whatever the plan gives. Concatenation through `SELECT @v = @v + col`, `TOP` without `ORDER BY` and ranking over non-unique keys behave the same way. The newer engine's plans can differ even at the same level.
2. **Wrong-result fixes (§2.3).** Optimizer fixes for wrong results apply at every level. A wrong result the client's unpatched server returns today is not reproduced. This cannot be detected from source.
3. **RC4 encryption returns `NULL` (§2.4).** On the client's server at level 100 it returns ciphertext, according to the documentation.
4. **Limits that used to raise errors (§2.5, §2.6).** `HASHBYTES` over more than 8,000 bytes and a nonclustered index key over 900 bytes raised errors on 2008 to 2014. They succeed in the sandbox. A `CATCH` block the client's code depends on is then skipped.
5. **Version and server metadata (§2.13, §2.14).** `@@VERSION`, `SERVERPROPERTY` and the shape of system views report the 2022 engine. Code that branches on them takes another path.
6. **Spatial and XML edge cases (§2.7, §2.9).** `STEnvelope()` of an empty geometry returns a different value. XML queries over long strings that returned wrong results before 2014 now return correct ones.
7. **Culture-dependent formatting (§2.15).** `FORMAT`, `PARSE` and `TRY_PARSE` use .NET culture data, which the container does not take from the client's Windows server.
8. **Error text (§2.12).** A `CATCH` block that stores or parses `ERROR_MESSAGE()` can record different text.

One point about §3 matters as much as any entry above. **The sandbox must run at exactly the client's level.** A level that is too high or too low can cross a boundary such as 130, and every `datetime` to `datetime2` conversion then changes (§3.20 to §3.31).

## 1. Removed: fails at every level

No compatibility level restores a discontinued feature. Microsoft states this, with `FASTFIRSTROW` as its example ([compatibility level][compat]). Every entry here failed on 2022 at levels 100 to 160 unless marked [docs] only. An artefact that uses one cannot run in the sandbox. In line with [ADR 0014](../adr/0014-reconciliation-never-fakes-what-it-cannot-run.md) it would be "Not reconcilable locally", not a failed conversion. ADR 0015's source-side test server covers only levels 80 and 90, so it does not help here as decided.

| ID | Construct | Before | On 2022, levels 100 to 160 | Old behaviour on | Parser detection | Source | Evidence |
|---|---|---|---|---|---|---|---|
| 1.1 | `COMPUTE` and `COMPUTE BY` | Extra summary result sets | Error 156, "Incorrect syntax near the keyword 'COMPUTE'" | 2008, 2008 R2 | The keyword `COMPUTE` at the end of a `SELECT` | [Discontinued 2012][disc14] | [docs] [ran] |
| 1.2 | `RAISERROR 50001 'text'` (integer then string, no parentheses) | Raises the error | Error 102 | 2008, 2008 R2 | `RAISERROR` followed by a literal, not `(` | [Discontinued 2012][disc14] | [docs] [ran] |
| 1.3 | Table hint `FASTFIRSTROW` | Optimises for the first row | Error 321 | 2008, 2008 R2 | The hint name | [Discontinued 2012][disc14], [compatibility level][compat] | [docs] [ran] |
| 1.4 | `sp_dboption` | Sets a database option | Error 2812, procedure not found | 2008, 2008 R2 | A call to `sp_dboption` | [Discontinued 2012][disc14] | [docs] [ran] |
| 1.5 | `SET DISABLE_DEF_CNST_CHK` | Accepted | Error 195, not a recognised `SET` option | 2008, 2008 R2 | The `SET` option name | [Discontinued 2012][disc14] | [docs] [ran] |
| 1.6 | `CREATE TRIGGER … WITH APPEND` | Accepted | Error 195, "'APPEND' is not a recognized option" | 2008, 2008 R2 | `WITH APPEND` in a trigger definition | [Discontinued 2012][disc14] | [docs] [ran] |
| 1.7 | `sp_dropalias`, `sys.database_principal_aliases` | Manage and list aliases | Errors 2812 and 208 | 2008, 2008 R2 | The procedure or view name | [Discontinued 2012][disc14] | [docs] [ran] |
| 1.8 | SQL Mail: `xp_sendmail`, `xp_startmail` and the other SQL Mail procedures | Sends mail | Error 2812 for the two tested | 2008, 2008 R2 | A call to an `xp_` SQL Mail procedure | [Discontinued 2012][disc14] | [docs] [ran] for the two named |
| 1.9 | `sp_addserver` for a remote server (without `'local'`) | Adds a remote server | Error 15663, "no longer supported" | 2008, 2008 R2 | `sp_addserver` without the `'local'` argument | [Discontinued 2012][disc14] | [docs] [ran] |
| 1.10 | `BACKUP … WITH PASSWORD` or `MEDIAPASSWORD` | Accepted | Error 3032 | 2008, 2008 R2 | The option in a `BACKUP` statement | [Discontinued 2012][disc14] | [docs] [ran] |
| 1.11 | `RESTORE … WITH DBO_ONLY` | Accepted | Error 155 | 2008, 2008 R2 | The option in a `RESTORE` statement | [Discontinued 2012][disc14] | [docs] [ran] |
| 1.12 | `sp_configure` with `'user instances enabled'`, `'user instance timeout'` or `'awe enabled'` | Reads or sets the option | Error 15123, option does not exist (first and third tested) | 2008, 2008 R2 | `sp_configure` with one of these names | [Discontinued 2012][disc14] | [docs] [ran] |
| 1.13 | Renamed or removed DMV columns, for example `sys.dm_os_sys_info.physical_memory_in_bytes`, `bpool_commited`, `sys.dm_os_memory_clerks.single_pages_kb`, `multi_pages_kb`, `sys.dm_os_workers.locale` | Returned | Error 207, invalid column name (first and third tested) | 2008, 2008 R2 | The old column names on those views; the full list is on the source page | [Breaking changes 2012][brk14], [compatibility level][compat] | [docs] [ran] |
| 1.14 | `ALTER TABLE ..schema.table` or `ALTER TABLE .database.schema.table` | Succeeded | Error 117, too many prefixes | 2008, 2008 R2 | `ALTER TABLE` with a name of more than two parts | [Breaking changes 2012][brk14] | [docs] [ran] |
| 1.15 | `CONVERT` from `datetimeoffset` with a style other than 0 or 1 | Converted | Error 9809 | 2008, 2008 R2 | `CONVERT(type, expr, style)` with a style literal other than 0 or 1, where `expr` is `datetimeoffset`. Needs the expression's type, from the catalog or a declaration | [Breaking changes 2012][brk14] | [docs] [ran] |
| 1.16 | A table or column named `NEXT` aliased `VALUE` without `AS`, for example `SELECT c FROM next value` | Ran | Error 102 | 2008, 2008 R2 | The tokens `NEXT VALUE` not followed by `FOR` | [Breaking changes 2012][brk14] | [docs] [ran] |
| 1.17 | PolyBase external data sources `TYPE = HADOOP` and external tables over them | Queried Hadoop or Azure storage | Not supported; must be recreated with the new connectors | 2016 | `CREATE EXTERNAL DATA SOURCE … TYPE = HADOOP`; `wasb[s]://` or `abfs[s]://` locations; queries over `sys.external_tables` entries | [Discontinued 2022][disc] | [docs]. The container has no PolyBase, so this was not run |
| 1.18 | CLR assemblies not signed for `UNSAFE ASSEMBLY`, under `clr strict security` | A `SAFE` assembly loaded without signing | All assemblies are treated as `UNSAFE`, and "any assemblies that aren't signed fail to load". A server option, not a level | 2008 to 2016 | Calls to CLR routines (`sys.assembly_modules` in the catalog), `CREATE ASSEMBLY` | [clr strict security][clrstrict], [Breaking changes 2017][brk17] | [docs]. [ran]: the option is 1 on 2019, 2022 and 2025, and 0 on the 2017 CU31 image, although the page gives 1 as the 2017 default |
| 1.19 | Stretch Database (`REMOTE_DATA_ARCHIVE`) | Moved cold rows to Azure | Discontinued in all versions from July 2024 | 2016 | `REMOTE_DATA_ARCHIVE` in table options; `sp_rda_*` procedures | [Discontinued][disc] | [docs] |

Two documented removals do not apply to this population. **`*=` and `=*` outer joins** and **table hints without `WITH`**, such as `FROM t HOLDLOCK`, stopped working at level 90 ([SQL Server 2008 R2 compatibility level][compat08]). A 2008 database at level 100 already rejected them. `FROM t HOLDLOCK` and `FROM t WITH HOLDLOCK` failed on 2017 to 2025 at every level with error 1018, and a `*=` join is a syntax error at level 100 on the same four versions (tested for ADR 0015). `FROM t (NOLOCK)`, a hint in parentheses without `WITH`, still works [ran].

## 2. Changed, and no compatibility level covers it

The same statement gives a different result, or a different error, on 2022 at every level. No entry in this section is gated, except where stated.

| ID | Construct | Old behaviour | On 2022, every level | Old behaviour on | Parser detection | Source | Evidence |
|---|---|---|---|---|---|---|---|
| 2.1 | `ORDER BY` with `TOP 100 PERCENT` in a view, derived table, CTE or inline function | Not a change since 2008. The order is not guaranteed on any version: `ORDER BY` "is used only to determine the rows that are returned by the `TOP` or `OFFSET` clause". The order seen depends on the plan | A view over five rows ordered `DESC` returned `1 2 3 4 5` at levels 100 to 160 on 2017 to 2025 | All | `TOP (100) PERCENT` with `ORDER BY` inside a view, derived table, CTE or inline function, and any query that reads one without its own `ORDER BY` | [CREATE VIEW][view] | [docs] [ran] |
| 2.2 | Other plan-dependent results: `SELECT @v = @v + col … ORDER BY`; `UPDATE … SET @v = col = expr`; `TOP` without `ORDER BY`; `ROW_NUMBER` and `RANK` over a non-unique order; `SELECT … INTO` with `IDENTITY()` and `ORDER BY`; `FOR XML PATH` concatenation without `ORDER BY` | Undefined. For variable assignment, "the order and frequency in which the assignments are processed is nondeterminant" | Undefined. The plan can differ from the client's even at the same level, through engine changes the level does not freeze. One run of the concatenation pattern on 2022 at level 100 returned the expected `cba`, which proves nothing either way | All | The patterns named, from syntax. Non-uniqueness of an order needs the catalog's keys | [SELECT @local_variable][selvar] | [docs] that it is undefined. [inferred] that the sandbox can differ |
| 2.3 | Query optimizer fixes for wrong results | The client's build may return a wrong result for some query | Such fixes "aren't protected by trace flag 4199" and are not optional, so they apply at every level | Every build before the fix | Not detectable from source: the affected queries are not known in advance | [compatibility level][compat] | [docs] |
| 2.4 | Encryption with an `RC4` or `RC4_128` symmetric key at level 100 | New data can be encrypted at levels 90 and 100 (the current documentation still says so) | At level 100 the key is created and opened without error, and `ENCRYPTBYKEY` returns `NULL`. An `AES_256` key in the same batch returned 52 bytes. At 110 and above `CREATE SYMMETRIC KEY … RC4` is error 102 | 2008 to 2016 at level 100 | `ALGORITHM = RC4` or `RC4_128` in `CREATE SYMMETRIC KEY`; `ENCRYPTBYKEY` with such a key | [CREATE SYMMETRIC KEY][symkey], [compatibility level][compat] | [docs] [ran] on 2017, 2019, 2022 and 2025. The docs and the run disagree; see "Not established" |
| 2.5 | `HASHBYTES` over more than 8,000 bytes | Error: "allowed input values are limited to 8000 bytes" | Returns the hash (32 bytes for `SHA2_256` over 9,000 bytes) | 2008 to 2014 | `HASHBYTES` whose argument is a `(max)` type. Needs the expression's type | [HASHBYTES][hashbytes] | [docs] [ran] |
| 2.6 | Nonclustered index key between 901 and 1,700 bytes | 900 bytes for all index types; a row whose key is longer fails | Up to 1,700 bytes. Creating the index on a `varchar(1800)` column gave only a warning, and inserting a 1,500-byte key succeeded | 2008 to 2014 | Not from code: from the catalog, a nonclustered index whose declared key width exceeds 900 bytes on a 2014 or earlier source | [Maximum capacity][capacity] | [docs] [ran] |
| 2.7 | `STEnvelope()` of an empty geometry or geography | Returns the same empty type, for example `POINT EMPTY` | `GEOMETRYCOLLECTION EMPTY` | 2008, 2008 R2 | Calls to `.STEnvelope()` | [Behavior changes 2012][beh14] | [docs] [ran] |
| 2.8 | `xml.value()` converting an XML number to a SQL Server number | Converted through `xs:string`, so overflow errors name `nvarchar` | Converts directly in some cases, so the error text differs | 2008 to 2012 | `.value()` with an integer, decimal or float target type | [Behavior changes 2014][beh14] | [docs]. Not reproduced: on untyped XML, 2022 still gave the old message ("The conversion of the nvarchar value '100000' overflowed an INT2 column") at every level |
| 2.9 | XML queries over strings longer than 4,020 characters | "can return incorrect results" | Correct results | 2008 to 2012 | `.query()`, `.value()`, `.exist()` or `.nodes()`. The string length is data, so only the method can be flagged | [Behavior changes 2014][beh14] | [docs] |
| 2.10 | Deterministic CLR scalar functions and CLR type methods | Evaluated on each call | Constant-folded. A function wrongly marked deterministic then gives a different result | 2008, 2008 R2 | Calls to CLR routines (`sys.assembly_modules`) | [Behavior changes 2012][beh14] | [docs] |
| 2.11 | Row count sent to the client for a failed DML statement inside `TRY…CATCH` | -1 in some cases | 0 | 2008, 2008 R2 | Not detectable from T-SQL: only the client sees it | [Breaking changes 2012][brk14] | [docs] |
| 2.12 | Error message text and numbers | Each version's own text | 2022's text. Examples: `DBCC CHECKIDENT … RESEED` no longer reports the current column value; the text of error 321 differs between 2017 and 2019 | Each version | `ERROR_MESSAGE()` stored, compared or parsed; `DBCC` output captured with `INSERT … EXEC` | [Behavior changes 2012][beh14] | [docs] [ran] |
| 2.13 | Version and server metadata: `@@VERSION`, `@@MICROSOFTVERSION`, `SERVERPROPERTY('ProductVersion')`, `'ProductMajorVersion'`, `'Edition'`, `xp_msver`, `DATABASEPROPERTY(db, 'Version')` | The client's version | The container's version at every level: `@@MICROSOFTVERSION` 268439751, major version 16, database version 957 on 2022 | All | Those names. Flag any branch on them | [Breaking changes 2012][brk14] for `Edition` values | [ran] |
| 2.14 | Shape and types of system objects. Catalog views and DMVs gain columns, sometimes not at the end; `RESTORE HEADERONLY` gained `KeyAlgorithm`, `EncryptorThumbprint` and `EncryptorType` (2014 CU1) and `LastValidRestoreTime`, `TimeZone` and `CompressionAlgorithm` (2022); `RESTORE FILELISTONLY` gained `SnapshotURL` (2016 CU1); `sys.dm_io_virtual_file_stats.sample_ms` and `sys.fn_virtualfilestats.timestamp` widened from `int` to `bigint` (2016); `sys.dm_exec_requests.command` widened (2012) | The client's shape | 2022's shape. `SELECT *` returns more columns, `INSERT … EXEC` into a fixed table fails with a column-count error, and assigning a widened column to an `int` can overflow | Each version | `SELECT *` from `sys.*` or `INFORMATION_SCHEMA`; `INSERT … EXEC` of `RESTORE … ONLY`, `DBCC … WITH TABLERESULTS` or system procedures; assignment from the widened columns | [Breaking changes 2012][brk14], [Breaking changes 2016][brk16], [RESTORE HEADERONLY][headeronly], [RESTORE FILELISTONLY][filelist] | [docs]; [ran] for `sample_ms` (`bigint`) |
| 2.15 | `FORMAT`, `PARSE`, `TRY_PARSE` | Formatted with the .NET Framework culture data on the client's Windows server | Formatted with the culture data of the container's runtime. `FORMAT(1234567.891, 'N', 'de-DE')` gave `1.234.567,89` and a Dutch long date gave `donderdag 12 mei 2011`; whether 2012 to 2016 give the same for every culture is not known | 2012 to 2016 (the functions arrived in 2012) | Calls to the three functions | [FORMAT][format] | [inferred]. [ran] on 2022 only |
| 2.16 | `CREATE CERTIFICATE` without a file (self-signed) | Signed with SHA1 | Signed with SHA2_256. Only the certificate's thumbprint and signature differ | 2008 to 2016 | `CREATE CERTIFICATE … WITH SUBJECT` | [Breaking changes 2017][brk17] | [docs] |

## 3. Covered by compatibility level

Each row is gated: the old behaviour holds below the level named. A 2008 or 2008 R2 database is natively at 100, 2012 at 110, 2014 at 120 and 2016 at 130, but any of them can be lower. The last column says whether 2022 at the level below gave the documented old behaviour. "Yes [ran]" means it did on 2022, and also on 2017, 2019 and 2025 wherever those accept the level.

The level tables in the [compatibility level][compat] reference are the source unless another is named.

### 3a. Language and functions

| ID | Construct | Below the level | At or above | Level | Parser detection | 2022 below the level gives the old behaviour |
|---|---|---|---|---|---|---|
| 3.1 | `SOUNDEX` | Older rules: `SOUNDEX('Pfister')` = `P123`, `SOUNDEX('Lloyd')` = `L430` | `P236`, `L300` | 110 | Calls to `SOUNDEX`, and `DIFFERENCE`, which uses it ([SOUNDEX][soundex]) | Yes [ran] |
| 3.2 | `datetime` or `smalldatetime` plus `date`, `time`, `datetime2` or `datetimeoffset` | Allowed: `'2020-01-01'` (as `datetime`) plus `'2020-01-02'` (as `date`) gave `2140-01-02` | Error 402 | 110 | `+` or `-` between those types. Needs operand types | Yes [ran] |
| 3.3 | Default style of `CAST` and `CONVERT` from `time` or `datetime2` in a computed column | Style 0: `Jun  7 2011  3:15PM` | Style 121: `2011-06-07 15:15:35` | 110 | `CAST` or `CONVERT` without a style, from `time` or `datetime2`, in a computed column definition | Yes [ran]. Outside computed columns the style is 121 at every level [ran] |
| 3.4 | XQuery `string-length` and `substring` on a surrogate pair; `fn:contains`, `fn:concat` and comparisons | Pair counted as 2 characters | Counted as 1 | 110 | XQuery strings in `.value()`, `.query()`, `.exist()` | Yes [ran] for `string-length`. Others [docs] ([Breaking changes 2012][brk14]) |
| 3.5 | `PIVOT` in a recursive CTE | Allowed, returns "incorrect results when there are multiple rows per grouping" | Error | 110 | `PIVOT` inside a recursive CTE | [docs] only |
| 3.6 | Reserved keywords `TRY_CONVERT`, `SEMANTICKEYPHRASETABLE`, `SEMANTICSIMILARITYTABLE`, `SEMANTICSIMILARITYDETAILSTABLE`, `WITHIN GROUP` | Usable as identifiers | Error 156 | 110 | These words as unquoted identifiers | Yes [ran] for `TRY_CONVERT` and `SEMANTICKEYPHRASETABLE`. `WITHIN` worked as an alias at every level, and as a table name at level 100, the only level tried, although the 2012 breaking-changes page says it became reserved ([Breaking changes 2012][brk14]) |
| 3.7 | Converting a `date` to a string with a language-dependent style | `SET LANGUAGE` ignored: `12 May 2011` | Language used: `12 mei 2011` in Dutch | 120 | `CONVERT` of a `date` with a style that names months or days, in code that runs `SET LANGUAGE` or under a non-English login | Yes [ran] |
| 3.8 | Recursive CTE with duplicate column names | Allowed | Error 8156 | 120 | Duplicate names in a recursive CTE's column list or anchor | Yes [ran] |
| 3.9 | Recursive reference on the right of `EXCEPT` in a recursive CTE | Infinite loop (stopped by `MAXRECURSION`, error 530) | Error 5336 | 120 | A recursive CTE whose recursive member uses `EXCEPT` with the CTE on the right | Yes [ran] |
| 3.10 | `ALTER TRIGGER` on a disabled trigger | Re-enables it | Keeps it disabled | 120 | `ALTER TRIGGER`, together with `DISABLE TRIGGER` anywhere | Yes [ran] |
| 3.11 | `OUTPUT … INTO` a table with an identity column, naming that column | Explicit value inserted although `IDENTITY_INSERT` is `OFF` | Error 544 | 120 | `OUTPUT … INTO t (cols)` where the column list includes `t`'s identity column | Yes [ran] |
| 3.12 | `MERGE … OUTPUT $action` in a partially contained database | Can raise a collation error | Uses the database collation | 120 | `$action` in a contained database | [docs] only |
| 3.13 | `STRING_SPLIT` | Error 208, object not found | Available | 130 | Calls to `STRING_SPLIT` (cannot occur in code that ran below 130) | Yes [ran] |
| 3.14 | Truncation error on insert | Error 8152 | Error 2628 with table, column and value | 150 (below it, 2628 needs trace flag 460) | `ERROR_NUMBER()` compared with 8152 | Yes [ran]. Sources are at 130 or below, so the sandbox gives 8152 as they do ([database scoped configuration][dbscoped]) |
| 3.15 | CLR objects | Run on CLR 4 with "some behavior changes introduced in version 4" avoided | CLR 4 behaviour | 110 | Calls to CLR routines | [docs] only |
| 3.16 | `smalldatetime` columns of remote tables in partitioned views | Mapped as `datetime` | Mapped as `smalldatetime` | 110 | Distributed partitioned views. They cannot run in the sandbox anyway | [docs] only |
| 3.17 | Hash algorithms | MD2, MD4, MD5, SHA and SHA1 "permitted" | "Only SHA2_256 and SHA2_512 hash algorithms are permitted" | 130 | `HASHBYTES` with an old algorithm name | Unclear. `HASHBYTES('MD5', 'a')` returned a hash at every level from 100 to 160, so the row does not apply to `HASHBYTES`. What it does apply to is not stated |
| 3.18 | `xml.exist()` on a `NULL` value compared with 0 | `WHERE @x.exist('/dogs') = 0` is true | False | 110 (found by test) | `.exist()` compared with 0 | Yes [ran]. Microsoft lists this as a 2012 behaviour change without naming a level ([Behavior changes 2012][beh14]). The tests show it is gated at 110 on 2017 to 2025 |
| 3.19 | Level 90 differences: `FOR BROWSE` in `INSERT`, two-way assignment `SET @v = col = expr`, variable assignment with a top-level `UNION`, `REPLACE` on `char` with trailing spaces, `DATEPART` on invalid literals, ODBC `{fn CONVERT()}`, `MERGE`, `CUBE` and `ROLLUP` as identifiers, and the others in the level 90 table | Level 90 behaviour | Level 100 behaviour | 100 | None needed | Not applicable: every source in scope is at 100 or above, so it already has the level 100 behaviour |

### 3b. Conversions and arithmetic at level 130

SQL Server 2016 changed several conversions and operations, and gated them at level 130 ([KB 4010261][kb130], [Breaking changes 2016][brk16]). The examples are Microsoft's, and all were run on 2022 at levels 100 to 160. Each gave the "below 130" value at 100 to 120 and the "130" value from 130.

These matter for 2008 to 2014 sources and for 2016 databases kept at 120 or below. Microsoft's article compares a 2016 engine at level 120 with the same engine at 130. That a 2008 to 2014 engine gives the "below 130" value is inferred, not documented.

| ID | Operation | Example | Below 130 | 130 and above | Parser detection |
|---|---|---|---|---|---|
| 3.20 | `float` or `numeric` to `datetime`, back to `float` | `DECLARE @d datetime = CAST(1.2 AS float)`, then `CAST(@d AS float)` | 1.19999996141975 | 1.2 | Implicit or explicit conversion between numeric types and `datetime` or `smalldatetime` |
| 3.21 | Negative `datetime` at a half day, to `int` | `CAST(CAST(-0.5 AS datetime) AS int)` | 0 | -1 | As 3.20 |
| 3.22 | `float` to `real` near the maximum | `CAST(3.40282347000E+038 AS real)` | Error 232, arithmetic overflow | 3.402823E+38 | Conversion to `real` |
| 3.23 | `float` to `numeric` | `CAST(CAST(0.14999999999999999 AS float) AS numeric(1, 1))` | 0.2 | 0.1 | Conversion from `float` or `real` to `numeric`, `decimal`, `money` |
| 3.24 | String of more than 39 characters to `decimal` | 40-character string to `decimal(2, 1)` | Error 8115 | 1.1 | Conversion from a string to `numeric` or `decimal` |
| 3.25 | String with a leading space to `bit` | `CAST(N' 1' AS bit)` | Error 245 | 1 | Conversion from a string to `bit` |
| 3.26 | `datetime` to `time` or `datetime2` | `'1900-01-01 00:00:00.003'` to `time(7)` | 00:00:00.0030000 | 00:00:00.0033333 | Conversion from `datetime` to `time` or `datetime2`, including implicit conversion in comparisons and assignments |
| 3.27 | `time` or `datetime2` to `datetime` | `time(4) '00:00:00.0045'` to `datetime` | .007 | .003 | Conversion to `datetime` |
| 3.28 | Comparing `datetime` with `datetime2` | `datetime '…00.003'` against the same value stored in `datetime2(3)` | Equal | Not equal | A comparison or join between a `datetime` and a `datetime2` expression. Needs types from the catalog and declarations. This is the commonest case in real code |
| 3.29 | `DATEPART` and `DATEDIFF` in microseconds or nanoseconds on `datetime` | `DATEPART(MICROSECOND, datetime '…00.003')` | 3000 | 3333 | `DATEPART` or `DATEDIFF` with `microsecond`, `mcs`, `nanosecond`, `ns` on a `datetime` |
| 3.30 | `DEGREES` and `RADIANS` on `numeric`; `numeric` addition and subtraction when one operand's scale exceeds the result's | `DEGREES(CAST(1 AS numeric))`; `numeric(38,2) -1.15 + numeric(38,1) 10` | 57.295779513082323000; 8.8 | 57.295779513082322865; 8.9 | Calls to `DEGREES`, `RADIANS`; `numeric` arithmetic with scales that differ |
| 3.31 | `ROUND` on `float`; `CONVERT` with a `NULL` style; `numeric` to `float` | `ROUND(CAST(-0.4175 AS float), 3)`; `CONVERT(smallint, '0', NULL)`; `numeric(18,10) 12345678.0123456781` to `float` | -0.418; 0; …22C4 | -0.417; `NULL`; …22C3 | `ROUND` on `float`; `CONVERT` with a third argument that can be `NULL`; conversion of `numeric` to `float` |

Microsoft also lists `float` to `money`, `datetime` to `float` and scale-0 `numeric` to `float` precision changes, which were not run.

### 3c. Query processing

Below their level the newer engine keeps the older behaviour for: the cardinality estimator (new models at 120, 130 and each later default level), query optimizer fixes released under trace flag 4199, parallel `SELECT … INTO` (120) and `INSERT … SELECT` (130), batch mode changes (130, 140, 150), adaptive joins and interleaved execution (140), scalar UDF inlining and table variable deferred compilation (150), and parameter-sensitive plans (160) ([compatibility level][compat]). These change plans, not documented results. They matter only through the plan-dependent constructs in §2.1 and §2.2. Scalar UDF inlining, which can change behaviour, starts at 150, above every source's level.

## 4. Checked and found to agree

These were checked because a page lists them as deprecated or discontinued, and the same statement gave the expected old result on 2022 at every level from 100 to 160 [ran].

- **Documented as discontinued, still working.** `DATABASEPROPERTY('master', 'IsAutoShrink')` returned 0 on 2017, 2019, 2022 and 2025, although Microsoft lists the function as discontinued in 2012 ([Discontinued 2012][disc14]). The version argument of `PWDCOMPARE`, listed as discontinued in 2012, was accepted. Neither should be flagged as a failure. The database version that `DATABASEPROPERTY` returns differs by engine (§2.13).
- **Deprecated, still working.** `SET ROWCOUNT` limiting a `DELETE`; result sets returned from a trigger (`disallow results from triggers` is 0); `@@REMSERVER` (returns `NULL`); `SET REMOTE_PROC_TRANSACTIONS`; numbered procedures (`p;2`); the `::fn_…()` call syntax; `GROUP BY ALL`; `SET CONCAT_NULL_YIELDS_NULL OFF`; `SET ANSI_NULLS OFF`; `text` with `TEXTPTR` and `READTEXT`; `sp_addtype`; `(NOLOCK)` without `WITH`; `HASHBYTES` with MD5 ([Deprecated 2016][dep16], [Deprecated 2017][dep17]). The 2019, 2022 and 2025 deprecation lists add nothing that applies to T-SQL artefacts ([Deprecated 2019][dep19], [Deprecated 2022][dep22], [Deprecated 2025][dep25]).
- **Collations.** A collation's name fixes its version: `COLLATIONPROPERTY(…, 'Version')` gave 0 for `Latin1_General_CI_AS` and `SQL_Latin1_General_CP1_CI_AS`, 2 for `Latin1_General_100_CI_AS` and 3 for `Japanese_XJIS_140_CI_AS` [ran]. Later versions add collations (3,955 on 2017; 5,508 on 2019 and 2022, with UTF-8; 5,540 on 2025 [ran]) but no page found says the rules of an existing collation changed. That they did not is [inferred]. The container's server collation must still match the client's ([sandbox fidelity](sandbox-fidelity.md), S2).
- **Removed only in 2025.** Full-text indexes built before 2025 fail after an upgrade to 2025 ([Breaking changes 2025][brk25]). This does not affect a 2022 container, but matters if the sandbox moves to 2025.
- **Discontinued features outside T-SQL artefacts.** SQL-DMO, the VIA protocol, 32-bit AWE, the ActiveX Agent subsystem, SSL, startup options `-h` and `-g`, renamed Extended Events, and the database-scoped options discontinued in 2019, which never existed on 2016 ([Discontinued][disc], [Discontinued 2012][disc14]).

## How the parser should use this

A construct is flagged with the entry ID and its category. For category 1 the artefact is not reconcilable in the sandbox. For category 2 the report says the sandbox result may differ from the client's server and names the entry. For category 3 nothing is flagged when the sandbox runs at the client's level, but the level must come from the snapshot (`sys.databases.compatibility_level`), never from the server version.

About half the entries need more than the token stream: the type of an expression (1.15, 2.5, 3.2, 3.20 to 3.31) or catalog facts such as index key widths and CLR modules (1.18, 2.6, 2.10). Where the type cannot be resolved, the safe choice is to flag.

## Not established

- **Any behaviour of a 2008 to 2016 engine, by test.** Every "old" value is Microsoft's. The build's Windows VMs should confirm, in order: §2.4 (RC4), §2.5, §2.6, §2.7, §3.18, §3.20 to §3.31 on a 2014 engine, and §1.13 to §1.16.
- **RC4 at level 100.** The current `CREATE SYMMETRIC KEY` page says new material can be encrypted with RC4 at levels 90 and 100. Every engine tested returns `NULL`. When this changed is not documented.
- **`xml.value()` conversion (§2.8).** The documented change was not reproduced with untyped XML. It may apply only to typed XML.
- **The level 130 hash algorithm row (§3.17).** What "permitted" applies to is not stated, and `HASHBYTES` is not affected.
- **`WITHIN` as a reserved word (§3.6).** The 2012 page says references to objects named `within` fail. On 2022 they work at every level.
- **The `clr strict security` default on 2017.** The 2017 CU31 image reports 0, against the documented default of 1.
- **Whether a 2016 engine below level 130 behaves exactly like a 2014 engine.** Microsoft's article on level 130 compares levels on the same engine only.
- **Plan-dependent constructs (§2.1, §2.2).** How often the 2022 plan at the client's level differs from the client's plan for the same data is not known, and cannot be found from documentation.
- **`FORMAT` and culture data (§2.15).** Whether the container's runtime gives the same output as .NET Framework on Windows for every culture.
- **Undocumented changes.** There is no breaking-changes page for 2022, the 2019 page lists none, and the 2014 page lists none for 2014. Changes made in cumulative updates and service packs of 2008 to 2016, such as trace flag 460 and wrong-result fixes, were not catalogued.
- **Errors raised at different points.** Whether a statement that fails in both versions fails at compile time in one and at run time in the other, which changes which statements of a batch run, was not examined.

[compat]: https://learn.microsoft.com/en-us/sql/t-sql/statements/alter-database-transact-sql-compatibility-level
[compat08]: https://learn.microsoft.com/en-us/previous-versions/sql/sql-server-2008-r2/bb510680(v=sql.105)
[disc]: https://learn.microsoft.com/en-us/sql/database-engine/discontinued-database-engine-functionality-in-sql-server
[disc14]: https://learn.microsoft.com/en-us/previous-versions/sql/2014/database-engine/discontinued-database-engine-functionality-in-sql-server-2016?view=sql-server-2014
[brk14]: https://learn.microsoft.com/en-us/previous-versions/sql/2014/database-engine/breaking-changes-to-database-engine-features-in-sql-server-2016?view=sql-server-2014
[beh14]: https://learn.microsoft.com/en-us/previous-versions/sql/2014/database-engine/behavior-changes-to-database-engine-features-in-sql-server-2014?view=sql-server-2014
[brk16]: https://learn.microsoft.com/en-us/previous-versions/sql/database-engine/breaking-changes-to-database-engine-features-in-sql-server-2016
[brk17]: https://learn.microsoft.com/en-us/sql/database-engine/breaking-changes-to-database-engine-features-in-sql-server-2017
[brk19]: https://learn.microsoft.com/en-us/sql/database-engine/breaking-changes-to-database-engine-features-in-sql-server-2019
[brk25]: https://learn.microsoft.com/en-us/sql/database-engine/breaking-changes-to-database-engine-features-in-sql-server-2025
[dep16]: https://learn.microsoft.com/en-us/previous-versions/sql/database-engine/deprecated-database-engine-features-in-sql-server-2016
[dep17]: https://learn.microsoft.com/en-us/sql/database-engine/deprecated-database-engine-features-in-sql-server-2017
[dep19]: https://learn.microsoft.com/en-us/sql/database-engine/deprecated-database-engine-features-in-sql-server-2019
[dep22]: https://learn.microsoft.com/en-us/sql/database-engine/deprecated-database-engine-features-in-sql-server-2022
[dep25]: https://learn.microsoft.com/en-us/sql/database-engine/deprecated-database-engine-features-in-sql-server-2025
[kb130]: https://learn.microsoft.com/en-us/troubleshoot/sql/database-engine/general/sql-server-azure-sql-database-improvements
[dbscoped]: https://learn.microsoft.com/en-us/sql/t-sql/statements/alter-database-scoped-configuration-transact-sql
[view]: https://learn.microsoft.com/en-us/sql/t-sql/statements/create-view-transact-sql
[selvar]: https://learn.microsoft.com/en-us/sql/t-sql/language-elements/select-local-variable-transact-sql
[symkey]: https://learn.microsoft.com/en-us/sql/t-sql/statements/create-symmetric-key-transact-sql
[hashbytes]: https://learn.microsoft.com/en-us/sql/t-sql/functions/hashbytes-transact-sql
[capacity]: https://learn.microsoft.com/en-us/sql/sql-server/maximum-capacity-specifications-for-sql-server
[clrstrict]: https://learn.microsoft.com/en-us/sql/database-engine/configure-windows/clr-strict-security
[format]: https://learn.microsoft.com/en-us/sql/t-sql/functions/format-transact-sql
[headeronly]: https://learn.microsoft.com/en-us/sql/t-sql/statements/restore-statements-headeronly-transact-sql
[filelist]: https://learn.microsoft.com/en-us/sql/t-sql/statements/restore-statements-filelistonly-transact-sql
[soundex]: https://learn.microsoft.com/en-us/sql/t-sql/functions/soundex-transact-sql

- Compatibility level: [ALTER DATABASE compatibility level][compat], [SQL Server 2008 R2 compatibility level][compat08], [KB 4010261: improvements at level 130][kb130], [ALTER DATABASE SCOPED CONFIGURATION][dbscoped]
- Discontinued: [current page, 2016 to 2025][disc], [2012 and 2014][disc14]
- Breaking and behaviour changes: [2008 to 2014][brk14], [behaviour changes 2012 and 2014][beh14], [2016][brk16], [2017][brk17], [2019][brk19], [2025][brk25]
- Deprecated: [2016][dep16], [2017][dep17], [2019][dep19], [2022][dep22], [2025][dep25]
- T-SQL reference: [CREATE VIEW][view], [SELECT @local_variable][selvar], [CREATE SYMMETRIC KEY][symkey], [HASHBYTES][hashbytes], [SOUNDEX][soundex], [FORMAT][format], [RESTORE HEADERONLY][headeronly], [RESTORE FILELISTONLY][filelist], [maximum capacity specifications][capacity], [clr strict security][clrstrict]
