# Parsers for sources: T-SQL, Oracle PL/SQL and SSIS packages

Research for [issue #2](https://github.com/Bach-To-The-Future/data-migration/issues/2). Findings as of 2026-10-13.

Every claim is marked **[ran]** (I executed it; versions and method are in [How the checks were run](#how-the-checks-were-run)) or **[docs]** (taken from the project's own documentation, source, licence or issue tracker, not executed).

## Answer

**No single parser covers every source. Each source needs its own reader, and none of them produces PySpark DataFrame code.**

- **T-SQL:** Microsoft ScriptDom is the only candidate that turned every procedural construct in the test artefacts into a typed tree with zero errors: procedure bodies, `IF`/`WHILE`, cursors, `TRY`/`CATCH`, transactions, temp tables, table variables, triggers, `MERGE`, and both forms of dynamic SQL call. It keeps source positions on every node and comments in a token stream. It is a .NET library, but it ran in-process from CPython through pythonnet. It reads only T-SQL and writes only T-SQL.
- **Oracle PL/SQL:** two candidates read the PL/SQL test artefacts into a real tree: the ANTLR grammars-v4 PL/SQL grammar (actively maintained, slow under the Python runtime) and sqlfluff's `oracle` dialect (pure Python, a few gaps). ScriptDom cannot read PL/SQL at all.
- **One parser for both SQL sources:** sqlfluff is the only candidate that is pure Python and read both procedural T-SQL and PL/SQL. Its tree is a linter's concrete syntax tree: expressions are flat token runs with no operator precedence, and it has no code generator. It is a credible single front end only if the core accepts doing its own expression structuring.
- **sqlglot is not a foundation for stored procedures.** Its procedural support is experimental and, by its maintainers' own statement, not a priority. On the test procedure it raised a parse error, and on simplified procedures it silently attached statements to the wrong block. It is, however, the only candidate that generates Snowflake SQL and Spark SQL, and it read every query-level statement cleanly. It fits as the query-level layer underneath a procedural reader, a combination that worked when tried.
- **SSIS `.dtsx`:** no SQL parser is involved and no maintained Python library was found. A `.dtsx` is XML with a published Microsoft specification, and the Python standard library read two real packages. That is enough for a recognised artefact type in v1.

## Comparison

"Tree" means the construct became typed nodes whose parts can be walked. "Opaque" means it became a single node holding raw text.

| | ScriptDom | grammars-v4 `TSql` | grammars-v4 `PlSql` | sqlfluff | sqlglot | sqlparser-rs (sqloxide) |
| --- | --- | --- | --- | --- | --- | --- |
| Version run | 180.140.0 | master @ 2026-10-09, ANTLR 4.13.2 | same | 4.4.0 | 30.22.0 | sqloxide 0.61.1 |
| Procedural T-SQL | Tree, 0 errors on all artefacts and 19 extra statements | Tree; 2 of 21 extra statements failed (`IF UPDATE(col)` in a trigger, `OPENQUERY`) | n/a | Tree, 0 unparsable sections on all artefacts and 20 extra statements | Parse error on the test procedure; opaque for `TRY`/`CATCH`, cursors, triggers; silent mis-nesting | Parse error on the test procedure, `TRY`/`CATCH`, `FETCH ... INTO @a, @b` |
| Procedural PL/SQL | Cannot read it | n/a | Tree; 1 failure in 17 extra statements | Tree; 3 unparsable in 17 extra statements | Parse error on the test procedure; packages and triggers opaque | Parse error on all three artefacts |
| Comments | Kept, in the token stream | Kept, on a hidden token channel | Kept, on a hidden token channel | Kept, as tree nodes | Kept "best effort", attached to nodes | Dropped |
| Source positions | Every node: line, column, offset, length | Every node, via its tokens | Every node, via its tokens | Every node | Identifiers and some functions only; statements carry none | "Work in progress" per its README |
| Generates Snowflake SQL | No (T-SQL only) | No generator | No generator | No generator | **Yes** | No |
| Toward PySpark DataFrame | Nothing | Nothing | Nothing | Nothing | No DataFrame output; Spark SQL output and a typed query tree to build an emitter on | Nothing |
| Language / runtime | C#, .NET; in-process from Python via pythonnet | Grammar; generated Python parser on `antlr4-python3-runtime` | same | Pure Python | Pure Python | Rust, Python binding |
| Licence | MIT | MIT (grammar), BSD-3-Clause (ANTLR) | Apache-2.0 (grammar header), BSD-3-Clause (ANTLR) | MIT | MIT | Apache-2.0; binding MIT |
| Maintenance | NuGet releases every 1 to 3 weeks; latest 2026-10-12 | Last grammar change 2024-04/05 | Commits 2026-10-03 and 2026-10-09 | Release 2026-10-02 | Commit 2026-10-12 | Commit 2026-10-02 |

Parse speed on the 60-line test procedure, same machine **[ran]**: ScriptDom 89 ms first call, 1 ms repeated (71 ms first call through pythonnet); sqlfluff 0.10 s; ANTLR `TSql` under Python 10.8 s first call, 1.2 s repeated; ANTLR `PlSql` on the 53-line PL/SQL procedure 9.9 s first call, 0.12 s repeated. sqlglot and sqloxide were not timed.

## Construct-by-construct results

### T-SQL **[ran]**

| Construct | ScriptDom | sqlfluff `tsql` | grammars-v4 `TSql` | sqlglot `tsql` | sqloxide `mssql` |
| --- | --- | --- | --- | --- | --- |
| `CREATE PROCEDURE` body | Tree | Tree | Tree | Tree only when each statement ends in `;`, and then mis-nested (below) | Error on unparenthesised parameters |
| `IF` / `ELSE` | Tree | Tree | Tree | `IfBlock`, but `IF c SET ...` reads `SET` as an alias and `END ELSE` becomes opaque | Tree |
| `WHILE` | Tree | Tree | Tree | `WhileBlock`, but `IF c BREAK` reads `BREAK` as an alias | Tree |
| `BEGIN TRY` / `BEGIN CATCH` | Tree | Tree | Tree | Opaque (3 `Command` nodes) | Error |
| `THROW` | Tree | Tree | Tree | Error with arguments; bare `THROW` read as a column | Not run |
| Cursor declare / open / fetch / close / deallocate | Tree | Tree | Tree | `DECLARE CURSOR` and `FETCH` opaque; `OPEN cur` read as `OPEN AS cur` | Declare tree; `FETCH` into two variables errors |
| `BEGIN` / `COMMIT` / `ROLLBACK TRANSACTION` | Tree | Tree | Tree | Tree; `SAVE TRANSACTION` errors | Not run |
| Temp table create / drop, `SELECT INTO #t` | Tree | Tree | Tree | Tree | Not run |
| Table variable | Tree | Tree | Tree | Tree | Tree |
| `MERGE` with `NOT MATCHED BY SOURCE`, `OUTPUT` | Tree | Tree | Tree | Tree | Tree |
| `EXEC sp_executesql`, `EXEC (@sql)` | Tree; a procedure call and a string execution are different node types | Tree | Tree | Tree; `EXEC ('...' + @x)` errors | Tree |
| `CREATE TRIGGER` | Tree, including `IF UPDATE(col)` | Tree | Tree, but `IF UPDATE(col)` is a syntax error | Opaque | Tree |
| Scalar function, inline and multi-statement table function | Tree | Tree | Tree | Inline tree; scalar opaque; multi-statement body cut off after its first statement | Tree (scalar and inline) |
| `CREATE VIEW` | Tree | Tree | Tree | Tree | Tree |
| `GO` batch separator | Tree | Tree | Tree | Error | Not run |

Other statements sqlglot rejected with a parse error: `RETURN 0`, `DELETE ... OUTPUT ... INTO`, `WAITFOR DELAY` **[ran]**.

sqlglot's silent mis-nesting, which is worse than an error because nothing reports it **[ran]**:

```sql
CREATE PROCEDURE dbo.p @a INT AS BEGIN IF @a > 1 BEGIN SELECT 1; END; SELECT 2; END;
```

returned no warning and placed `SELECT 2` inside the `IF` block, as a sibling of `SELECT 1`. Without the `;` after the inner `END`, which is ordinary T-SQL style, `END SELECT 2` became one opaque `Command`. On the full test procedure, once the rejected `RETURN 0` was removed, everything from the cursor loop to the end of the `CATCH` block, including the `MERGE` and the `COMMIT`, ended up nested under one `IF` inside the `WHILE`.

This matches what sqlglot's maintainers say **[docs]**:

- 2023, a collaborator on a PL/SQL parse failure: "This isn't in SQLGlot's scope, as it mostly aims to cover standard SQL (or at least SQL dialects that maintain the declarative nature of the language)." ([issue #1356](https://github.com/tobymao/sqlglot/issues/1356))
- 2026-01-27, the owner, asked whether T-SQL procedural constructs will be supported: "perhaps, we have some experimental stuff right now but we're not going to prioritize it at the moment". ([issue #6868](https://github.com/tobymao/sqlglot/issues/6868))
- 2026-02, the change that added T-SQL procedure parsing splits the body on semicolons: "Each statement of the procedure is expected to have a semicolon at the end." ([PR #6955](https://github.com/tobymao/sqlglot/pull/6955))
- Unsupported syntax falls back to an opaque node with a log warning: "contains unsupported syntax. Falling back to parsing as a 'Command'." ([`sqlglot/parser.py`](https://github.com/tobymao/sqlglot/blob/v30.22.0/sqlglot/parser.py), line 2302 in the 30.22.0 wheel). `GO` and `PRINT` are tokenised as commands ([`sqlglot/dialects/tsql.py`](https://github.com/tobymao/sqlglot/blob/v30.22.0/sqlglot/dialects/tsql.py), lines 168 to 192).

### Oracle PL/SQL **[ran]**

| Construct | sqlfluff `oracle` | grammars-v4 `PlSql` | sqlglot `oracle` | sqloxide |
| --- | --- | --- | --- | --- |
| `CREATE PROCEDURE` with cursor, `%TYPE`, collection type, exception declarations | Tree | Tree | Error at the first `%TYPE` declaration | Error |
| `IF` / `ELSIF`, cursor `FOR` loop, `WHILE`, `EXIT WHEN` | Tree | Tree | Error, or garbage (`WHILE v < 10 LOOP` reads `LOOP` as an alias) | Not reached |
| `BULK COLLECT`, `FORALL` | Tree | Tree | `SELECT ... BULK COLLECT INTO` alone parses; inside a procedure not reached | Not reached |
| `EXECUTE IMMEDIATE` | Tree | Tree | Opaque | Not reached |
| `EXCEPTION` handlers, `RAISE` | Tree, but the handler is bare keywords inside the block, not its own node | Tree, `exception_handler` nodes | Error | Not reached |
| `MERGE`, `CONNECT BY`, `(+)` joins | Tree | Tree | Tree | Not run |
| Package body with function and procedure, `PRAGMA AUTONOMOUS_TRANSACTION` | Tree | Tree | Opaque | Error |
| Row-level trigger with `:NEW` | Tree | Tree | Opaque | Error (`oracle` dialect) |
| Anonymous block | Tree | Tree | Error | Not run |

A one-line `CREATE PROCEDURE p (...) IS BEGIN NULL; END p;` did come back from sqlglot as a `Create` node with no warning, but its body was nonsense (`IS AS BEGIN`, `END AS p`) **[ran]**.

Extra PL/SQL statements, 17 each **[ran]**:

- sqlfluff could not parse three: `SAVEPOINT` / `ROLLBACK TO` inside a block, a `PIPELINED` function with `PIPE ROW`, and `GOTO` with a `<<label>>`.
- grammars-v4 `PlSql` failed one: a compound trigger named `trg`, reporting `mismatched input 'trg'`. The same grammar accepted a trigger named `trg_orders_audit`, so the name looks like the cause, but that was not investigated.

## Candidates in detail

### Microsoft ScriptDom

- A .NET library from Microsoft: "SQL Script DOM is a .NET library that provides formatting and parsing capabilities to analyze SQL Scripts", MIT licensed, built with ANTLR **[docs]** ([README](https://github.com/microsoft/SqlScriptDOM/blob/main/README.md)).
- NuGet package `Microsoft.SqlServer.TransactSql.ScriptDom` 180.140.0 was published 2026-10-12; the five before it are dated 2026-09-23, 09-09, 08-21, 07-30 and 07-22. It targets .NET 8.0, .NET Standard 2.0 and .NET Framework 4.7.2, with no dependencies **[docs]** ([NuGet](https://www.nuget.org/packages/Microsoft.SqlServer.TransactSql.ScriptDom)).
- The assembly holds one parser class per SQL Server version from `TSql80Parser` to `TSql180Parser`, plus `TSqlFabricDWParser`, and 1077 public tree-node classes **[ran]**. API reference: [Microsoft.SqlServer.TransactSql.ScriptDom](https://learn.microsoft.com/en-us/dotnet/api/microsoft.sqlserver.transactsql.scriptdom) **[docs]**.
- Dynamic SQL is distinguishable in the tree: `EXEC sp_executesql ...` is an `ExecutableProcedureReference` carrying the procedure name, and `EXEC (@sql)` is an `ExecutableStringList`. The SQL inside the string is not parsed **[ran]**.
- Every node exposes `StartLine`, `StartColumn`, `StartOffset`, `FragmentLength` and first and last token indexes. Comments are tokens of type `SingleLineComment` and `MultilineComment` in the tree's `ScriptTokenStream`, each with line, column and offset **[ran]**.
- Its script generators exist only for T-SQL family versions (`Sql80ScriptGenerator` to `Sql180ScriptGenerator`, `SqlFabricDWScriptGenerator`, `SqlServerlessScriptGenerator`), and regenerating a script dropped its comments **[ran]**. It offers nothing toward Snowflake SQL or PySpark.
- From Python: `pythonnet.load("coreclr")`, `clr.AddReference(<dll>)`, then `TSql180Parser(True).Parse(StringReader(sql), None)` returned the tree and the error list; node properties were readable and a `TSqlFragmentVisitor` subclass written in Python was called for all 272 nodes **[ran]** on Windows 11, Python 3.12.10, .NET 10.0.12, pythonnet 3.2.1. pythonnet's default runtime on Windows is .NET Framework and .NET Core must be selected explicitly and be version 3.1 or later **[docs]** ([pythonnet docs](https://pythonnet.github.io/pythonnet/python.html)).
- It rejects the `?` parameter markers that SQL embedded in SSIS packages uses ("Incorrect syntax near '?'") **[ran]**.

### ANTLR grammars-v4: `sql/tsql` and `sql/plsql`

- Community grammars in [antlr/grammars-v4](https://github.com/antlr/grammars-v4). They are grammars only: parsing gives a concrete parse tree of rule contexts, and there is no generator and no transpiler **[docs]**.
- The T-SQL grammar's own README: "This is a community-supported grammar file for t-sql. It's not fully complete so far" **[docs]** ([README](https://github.com/antlr/grammars-v4/blob/master/sql/tsql/README.md)). Its file header is MIT. The last commits that changed the grammar are from April and May 2024; the only later commit on `TSqlParser.g4` is repository-wide housekeeping **[docs]** ([history](https://github.com/antlr/grammars-v4/commits/master/sql/tsql)).
- The PL/SQL grammar's README: "This grammar is for recognizing the latest version of PL/SQL" and "The grammar is ambiguous, but generally performs well" **[docs]** ([README](https://github.com/antlr/grammars-v4/blob/master/sql/plsql/README.md)). Its file header is Apache-2.0. It had commits on 2026-10-03 and 2026-10-09 **[docs]** ([history](https://github.com/antlr/grammars-v4/commits/master/sql/plsql)). It needs two small base classes, which the repository supplies for Python, and a mechanical rewrite of `this.` to `self.` before generation **[ran]**.
- ANTLR itself is BSD-3-Clause; the latest release is 4.13.2 from 2024-08-03 **[docs]** ([antlr/antlr4](https://github.com/antlr/antlr4)). The generator is a Java program, so building the parser needs a JVM; running it needs only the `antlr4-python3-runtime` package, whose version must match the generator's **[docs]** ([Python target](https://github.com/antlr/antlr4/blob/master/doc/python-target.md)).
- Generated Python parser files are 4.5 MB (`TSqlParser.py`) and 7.0 MB (`PlSqlParser.py`) **[ran]**.
- Comments arrive as tokens on a hidden channel with line, column and offsets; every rule node has start and stop tokens **[ran]**.
- The Python runtime is slow: about 10 s for the first parse in a process and 1.2 s for each repeat of a 60-line T-SQL procedure **[ran]**. [speedy-antlr-tool](https://github.com/amykyta3/speedy-antlr-tool) exists to run the parser through ANTLR's C++ target and hand back a Python tree **[docs]**; it was not tried.

### sqlfluff

- A SQL linter, MIT, pure Python; 4.4.0 was released 2026-10-02 **[docs]** ([repository](https://github.com/sqlfluff/sqlfluff)). `tsql` and `oracle` are two of its 28 dialects **[docs]** ([dialect reference](https://docs.sqlfluff.com/en/stable/reference/dialects.html)).
- A Rust parser exists as an opt-in extra, `sqlfluff[rs]`, described as beta **[docs]** ([4.0.0 release notes](https://github.com/sqlfluff/sqlfluff/releases/tag/4.0.0)). The checks here used the default Python parser.
- It parsed every T-SQL and PL/SQL test artefact with no unparsable section, and handles `GO` batches and the SQL*Plus `/` terminator **[ran]**.
- Nesting was correct where sqlglot's was not: the `IF ... BEGIN ... END; WHILE ...` case came back as two sibling statements **[ran]**.
- The tree is concrete and lint-oriented **[ran]**:
  - Whitespace, comments, keywords and punctuation are all nodes, each with line, column and source offsets.
  - Expressions are flat. `a + b * c - 1` is one `expression` node holding operands and operators in source order, with no precedence structure. `a > 1 AND (b = 2 OR c = 3)` is likewise flat apart from the bracket.
  - Some compound constructs are not subdivided. `try_catch` is one node containing the keywords and the statements of both halves in sequence. A PL/SQL `EXCEPTION WHEN OTHERS THEN` appears as bare keywords inside the enclosing block.
- It has no generator and no transpiler. It contributes nothing toward Snowflake SQL or PySpark.

### sqlglot

- "SQLGlot is a no-dependency SQL parser, transpiler, optimizer, and engine." T-SQL, Oracle, Snowflake, Spark and Databricks are all in its "Official Dialects" tier **[docs]** ([README](https://github.com/tobymao/sqlglot/blob/main/README.md)). MIT, Python 3.9 or later, version 30.22.0 **[docs]** ([PyPI](https://pypi.org/project/sqlglot/)).
- Query-level statements are its strength. Every `SELECT`, `INSERT`, `UPDATE ... FROM`, `MERGE`, CTE, `PIVOT`, `CROSS APPLY`, view, temp-table and table-variable statement tried in both dialects produced a typed tree; the one query-level failure was T-SQL `DELETE ... OUTPUT ... INTO` **[ran]**.
- It generates Snowflake SQL, Spark SQL and Databricks SQL from that tree. For example T-SQL `TOP 10`, `ISNULL`, `CONVERT(..., 120)`, `GETDATE()` and `CROSS APPLY` came out as `LIMIT 10`, `COALESCE`, `TO_CHAR`, `CURRENT_TIMESTAMP()` and `INNER JOIN LATERAL` **[ran]**.
- Generation is not a correctness check. Observed **[ran]**:
  - T-SQL `WHEN NOT MATCHED BY SOURCE THEN DELETE` became `WHEN NOT MATCHED THEN DELETE` for Snowflake, with nothing logged.
  - Oracle `(+)` joins were dropped for Spark, turning an outer join into an inner join, with a log warning only. Passing `unsupported_level=ErrorLevel.RAISE` turned that into an exception.
  - Oracle `CONNECT BY` and `ROWNUM` passed through unchanged to Spark.
  - A T-SQL `CREATE PROCEDURE` written to Snowflake came out as `CREATE PROCEDURE  AS BEGIN SET NOCOUNT = ON`, with the name and the rest of the body gone.
- Comments are "preserved on a best-effort basis" **[docs]** (README). They attach to the nearest node and are re-emitted as `/* ... */` **[ran]**.
- Positions are partial: in a five-line query, 13 of 27 nodes carried `line`, `col`, `start`, `end`, and all 13 were identifiers or a function call. `Select` and `Where` carried none **[ran]**.
- It does not produce PySpark DataFrame code. The `sqlglot.dataframe` module is absent from 30.22.0 **[ran]**; it was removed in May 2024 "in favor of sqlframe" **[docs]** ([PR #3518](https://github.com/tobymao/sqlglot/pull/3518)). [SQLFrame](https://github.com/eakmanrq/sqlframe) goes the other way: it "implements the PySpark DataFrame API" and turns DataFrame code into SQL **[docs]**.
- What it does offer toward DataFrame output is a typed query tree with named parts (`from_`, `joins`, `where`, `group`, `having`, `order`) and scope analysis that resolved the tables of a joined query **[ran]**. An emitter would have to be written.

### sqlparser-rs through sqloxide

- A Rust SQL parser, Apache-2.0, with a Python binding ([sqloxide](https://github.com/wseaton/sqloxide), MIT). Its README says spans are a "work in progress" and round-tripped SQL comes back "with comments removed" **[docs]** ([README](https://github.com/apache/datafusion-sqlparser-rs/blob/main/README.md)).
- It has real T-SQL `IF`, `WHILE`, trigger and function nodes, but failed the test procedure, `TRY`/`CATCH` and multi-variable `FETCH`, and failed all three PL/SQL artefacts **[ran]**. Dropping comments rules it out for the conversion report regardless.

### Not run, documentation only

- **Databricks Lakebridge.** Its Morpheus transpiler is "AST-based", uses a "custom parser generated from a carefully crafted ANTLR grammar", reads `mssql`, `snowflake` and `synapse`, targets only Databricks SQL, and does not support Oracle ([Morpheus](https://databrickslabs.github.io/lakebridge/docs/transpile/pluggable_transpilers/morpheus/)). BladeBridge is configuration-driven and covers Oracle and SQL Server, turning stored procedures into Databricks SQL scripting ([BladeBridge](https://databrickslabs.github.io/lakebridge/docs/transpile/pluggable_transpilers/bladebridge/)). SSIS is listed as a source ([transpile overview](https://databrickslabs.github.io/lakebridge/docs/transpile/)). The repository is under the "Databricks License", which permits use only "in connection with your use of the Databricks Services" ([LICENSE](https://github.com/databrickslabs/lakebridge/blob/main/LICENSE)). That restriction conflicts with adding a Snowflake target, and these are tools, not parser libraries.
- **ZPA.** A "parser and static code analysis tool for PL/SQL and Oracle SQL"; its README says classes outside the plugin API "are not prepared for external consumption" ([README](https://github.com/felipebz/zpa)). Kotlin, LGPL-3.0, release 4.2.1 on 2026-10-10 (GitHub metadata).
- **IslandSQL.** An ANTLR 4 parser for Oracle SQL and PL/SQL and PostgreSQL, needing a JVM 11 or later, Apache-2.0 ([README](https://github.com/IslandSQL/IslandSQL)). Release 0.25.0 on 2026-08-21 (GitHub metadata).
- **SnowConvert AI.** Snowflake's own converter from SQL Server, Oracle and others to Snowflake SQL, subject to its "Conversion Software Terms of Use" ([about](https://docs.snowflake.com/en/migrations/snowconvert-docs/general/about)). A tool, not a library.

## SSIS `.dtsx` packages

- The format is specified by Microsoft: [MS-DTSX] is "an XML-based file format that stores the instructions for the processing of a data flow", current revision 11.0 dated 2025-10-31, and [MS-DTSX2] specifies "version 2" of the format, revision 13.0 of the same date **[docs]** ([MS-DTSX](https://learn.microsoft.com/en-us/openspecs/sql_data_portability/ms-dtsx/235600e9-0c13-4b5b-a388-aa3c65aec1dd), [MS-DTSX2](https://learn.microsoft.com/en-us/openspecs/sql_data_portability/ms-dtsx2/fb216aa4-62ab-41c8-a6d5-5b1002739d21)).
- The SQL of an Execute SQL task lives in a `SqlTaskData` element in the `SQLTask` namespace **[docs]** ([SqlTaskData element](https://learn.microsoft.com/en-us/openspecs/sql_data_portability/ms-dtsx/b7f8b3a8-158f-4798-ba60-9bf6f4850cc8)).
- Python's `xml.etree.ElementTree` read two public packages, one with `PackageFormatVersion` 6 and one with 8 **[ran]**. From each it listed the package name, every executable by type (Execute SQL tasks, sequence containers, for-each loops, expression tasks), connection managers, precedence constraints, variables, and the text of each `SQLTask:SqlStatementSource` attribute: 4 statements in one package, 39 in the other.
- No maintained Python library was found. Fifteen plausible PyPI names (`dtsx`, `pydtsx`, `ssis`, `ssis-parser` and similar) do not exist. A GitHub search returned five Python projects with 0 to 2 stars each; the most-starred reader of any language was a C# project with 44 stars, last pushed in 2022 **[ran]**.
- Microsoft's own loader is the `Microsoft.SqlServer.ManagedDTS` assembly, `Application.LoadPackage` **[docs]** ([loading a package programmatically](https://learn.microsoft.com/en-us/sql/integration-services/run-manage-packages-programmatically/loading-and-running-a-local-package-programmatically)). It was not tried and is not needed to recognise a package.
- Two things a reader will meet:
  - Packages saved with protection level `EncryptAllWithPassword` or `EncryptAllWithUserKey` have "the whole package" encrypted, so their contents cannot be listed without the password or the original user's key **[docs]** ([protection levels](https://learn.microsoft.com/en-us/sql/integration-services/security/access-control-for-sensitive-data-in-packages)).
  - Embedded SQL uses `?` parameter markers, as in `EXEC Integration.GetLineageKey ?, ?;` from one of the sample packages. ScriptDom and sqlfluff both reject them; sqlglot accepts them **[ran]**.

## Combining readers

The ticket asks whether one parser can serve all sources. Since it cannot, the cheapest combination was tried **[ran]**: ScriptDom read the T-SQL test procedure, a Python visitor collected each `INSERT`, `SELECT` and `MERGE` node, the source text of each was sliced out using `StartOffset` and `FragmentLength`, and sqlglot parsed each slice. All four slices parsed to typed trees and generated Snowflake SQL. So a procedural reader supplying the skeleton and positions, with sqlglot reading the queries inside it, is workable on this evidence.

## How the checks were run

Windows 11, Python 3.12.10 in a throwaway virtual environment, .NET SDK 10.0.401, Java 21.

- **Packages:** sqlglot 30.22.0 (pure-Python wheel, no `c` extra), sqlfluff 4.4.0 (no `rs` extra), antlr4-python3-runtime 4.13.2, sqloxide 0.61.1, pythonnet 3.2.1, and NuGet `Microsoft.SqlServer.TransactSql.ScriptDom` 180.140.0 using `TSql180Parser`.
- **ANTLR parsers:** generated with `antlr4-4.13.2-complete.jar -Dlanguage=Python3 -visitor` from `TSqlLexer.g4`, `TSqlParser.g4`, `PlSqlLexer.g4` and `PlSqlParser.g4` as they stood on `master` on 2026-10-13.
- **Test artefacts,** all written for this ticket and therefore small and synthetic:
  - a 60-line T-SQL stored procedure (below);
  - a T-SQL `AFTER INSERT, UPDATE` trigger using `IF UPDATE(Amount)` and the `inserted` table;
  - a T-SQL scalar function, inline table function and a view using `CROSS APPLY`;
  - a 53-line PL/SQL procedure (below);
  - a PL/SQL package body with one function and one procedure using `PRAGMA AUTONOMOUS_TRANSACTION`;
  - a PL/SQL row-level trigger using `UPDATING` and `:NEW`.
- **Extra statements:** 19 to 21 single T-SQL statements, depending on the parser (`SAVE TRANSACTION`, `THROW` with arguments, `DELETE ... OUTPUT INTO`, `WAITFOR`, `GOTO`, `PIVOT`, `EXECUTE` of a concatenated string, `MERGE ... OUTPUT`, `OPENQUERY`, XML methods, `FOR XML PATH`, `STRING_AGG ... WITHIN GROUP`, `CREATE OR ALTER`, `INSTEAD OF` trigger and others) and 17 PL/SQL statements (anonymous block, package specification, explicit cursor loop, `%ROWTYPE` and record types, `CASE` statement, savepoints, pipelined function, `REF CURSOR`, `CONNECT BY`, `(+)` join, `GOTO`, compound trigger, `BULK COLLECT ... LIMIT`, nested exception block, object type).
- **"Tree" was judged** by walking the result and counting node types, and for sqlglot and sqlfluff by printing the tree. For sqlfluff, "no unparsable section" shows the grammar matched; tree shape was inspected by eye for `IF`/`WHILE` nesting, `TRY`/`CATCH`, cursors, a trigger and a PL/SQL loop with an exception handler, not for every statement.
- **SSIS samples:** `BulkLoadData.dtsx` from `rohitsies/GNAF-SSIS-Data-Loader` and `Chapter10/Recipe4/Recipe4.dtsx` from `PacktPublishing/ETL-with-Azure-Cookbook`.
- **Maintenance dates and licences** were read from the GitHub API on 2026-10-13.

T-SQL test procedure:

```sql
-- Header comment: rebuilds the daily order summary
CREATE PROCEDURE dbo.usp_RebuildOrderSummary
    @FromDate DATE,
    @Threshold INT = 10 /* default threshold */
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @CustomerId INT, @Total MONEY, @sql NVARCHAR(MAX);
    DECLARE @Seen TABLE (CustomerId INT PRIMARY KEY, Total MONEY);

    CREATE TABLE #Staging (CustomerId INT, Total MONEY);

    INSERT INTO #Staging (CustomerId, Total)
    SELECT o.CustomerId, SUM(o.Amount)
    FROM dbo.Orders AS o
    WHERE o.OrderDate >= @FromDate
    GROUP BY o.CustomerId;

    BEGIN TRY
        BEGIN TRANSACTION;

        DECLARE cur CURSOR LOCAL FAST_FORWARD FOR
            SELECT CustomerId, Total FROM #Staging;
        OPEN cur;
        FETCH NEXT FROM cur INTO @CustomerId, @Total;
        WHILE @@FETCH_STATUS = 0
        BEGIN
            IF @Total > @Threshold
            BEGIN
                -- big customer
                INSERT INTO @Seen (CustomerId, Total) VALUES (@CustomerId, @Total);
            END
            ELSE
                PRINT 'skipped';
            FETCH NEXT FROM cur INTO @CustomerId, @Total;
        END
        CLOSE cur;
        DEALLOCATE cur;

        MERGE dbo.OrderSummary AS t
        USING @Seen AS s ON t.CustomerId = s.CustomerId
        WHEN MATCHED THEN UPDATE SET t.Total = s.Total
        WHEN NOT MATCHED BY TARGET THEN INSERT (CustomerId, Total) VALUES (s.CustomerId, s.Total)
        WHEN NOT MATCHED BY SOURCE THEN DELETE;

        SET @sql = N'UPDATE dbo.Audit SET LastRun = GETDATE() WHERE Name = @n';
        EXEC sp_executesql @sql, N'@n sysname', @n = N'summary';
        EXEC (@sql);

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;
        THROW;
    END CATCH

    DROP TABLE #Staging;
    RETURN 0;
END
GO
```

PL/SQL test procedure:

```sql
-- Header comment: rebuilds the order summary
CREATE OR REPLACE PROCEDURE rebuild_order_summary (
    p_from_date IN DATE,
    p_threshold IN NUMBER DEFAULT 10 /* default threshold */
) IS
    CURSOR c_orders IS
        SELECT customer_id, SUM(amount) AS total
        FROM orders
        WHERE order_date >= p_from_date
        GROUP BY customer_id;
    v_total  orders.amount%TYPE;
    v_sql    VARCHAR2(4000);
    TYPE t_ids IS TABLE OF orders.customer_id%TYPE INDEX BY PLS_INTEGER;
    l_ids    t_ids;
    e_too_big EXCEPTION;
BEGIN
    FOR r IN c_orders LOOP
        IF r.total > p_threshold THEN
            -- big customer
            INSERT INTO seen (customer_id, total) VALUES (r.customer_id, r.total);
        ELSIF r.total = 0 THEN
            NULL;
        ELSE
            DBMS_OUTPUT.PUT_LINE('skipped');
        END IF;
    END LOOP;

    SELECT customer_id BULK COLLECT INTO l_ids FROM seen;
    FORALL i IN 1 .. l_ids.COUNT
        UPDATE order_summary SET flagged = 'Y' WHERE customer_id = l_ids(i);

    MERGE INTO order_summary t
    USING seen s ON (t.customer_id = s.customer_id)
    WHEN MATCHED THEN UPDATE SET t.total = s.total
    WHEN NOT MATCHED THEN INSERT (customer_id, total) VALUES (s.customer_id, s.total);

    v_sql := 'UPDATE audit_log SET last_run = SYSDATE WHERE name = :n';
    EXECUTE IMMEDIATE v_sql USING 'summary';

    WHILE v_total < 10 LOOP
        v_total := v_total + 1;
        EXIT WHEN v_total > 5;
    END LOOP;

    COMMIT;
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        ROLLBACK;
    WHEN OTHERS THEN
        ROLLBACK;
        RAISE;
END rebuild_order_summary;
/
```

## Not established

- **Coverage on real artefacts.** Every result comes from small artefacts written for this ticket. None of the parsers was run against a real legacy codebase, so failure rates on production T-SQL or PL/SQL are unknown.
- **ScriptDom from Python off Windows.** The pythonnet route was run only on Windows with .NET 10. The package targets .NET 8 and .NET Standard 2.0, which suggests Linux and macOS will work, but that was not run.
- **sqlfluff's tree as a stable interface.** No statement was found on whether the parse tree's node types are a supported, versioned interface; the API documentation page could not be retrieved.
- **ANTLR speed with a faster runtime.** Only the pure-Python runtime was timed. Neither speedy-antlr-tool nor a JVM sidecar was tried.
- **Why grammars-v4 `PlSql` rejected a trigger named `trg`.**
- **Whether sqlglot's compiled build (`sqlglot[c]`) parses identically.** Only the pure-Python wheel was run.
- **SSIS beyond Execute SQL tasks.** Neither sample package contained a data flow or a script task, so reading those was not exercised. Encrypted packages were not tested. Which SQL Server versions [MS-DTSX] and [MS-DTSX2] each cover was not established from the pages read.
- **Lakebridge internals.** Whether Morpheus or BladeBridge can be used as a library, and which transpiler handles SSIS, was not established. Neither was run.
- **Commercial parsers** such as General SQL Parser, and JSqlParser, were not evaluated.
- **SQL Agent jobs** are outside this ticket and were not investigated.

## What this means for other tickets

- **"What representation does the core work on?"** No parser's tree can serve as the core's representation. No one parser reads every source, and the four viable trees are structurally unlike each other: ScriptDom's 1077 typed .NET classes, ANTLR rule contexts, sqlfluff segments with flat expressions, and sqlglot expressions. Only sqlglot's tree has generators for a second target. Whatever the core works on, each source needs an adapter into it.
- **Runtime dependencies.** Choosing ScriptDom adds a .NET runtime to a Python tool. Choosing the ANTLR grammars adds a JVM at build time and a slow parser at run time unless a faster runtime is adopted.
- **The conversion report cannot rely on sqlglot to flag what it could not translate.** It silently changed the meaning of a `MERGE` and emitted a hollow procedure. Constructs sqlglot cannot carry to a target have to be detected before generation.
- **Recognising dynamic SQL** is straightforward with ScriptDom, which separates string execution from procedure calls. No parser looks inside the string.
- **If SSIS later becomes a converted artefact type,** its embedded SQL needs its `?` markers handled before a T-SQL parser will accept it.
