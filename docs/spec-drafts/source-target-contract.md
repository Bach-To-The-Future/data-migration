# What a source and a target provide

Draft for the spec, from [What must a new source or target provide?](https://github.com/Bach-To-The-Future/data-migration/issues/17), 17 October 2026. It states the contract each source and each target implements, then walks Oracle PL/SQL (a second source) and Snowflake SQL (a second target) through it on paper. Nothing for Oracle or Snowflake is built in v1.

Terms are defined in `CONTEXT.md`. The operation vocabulary is in [`operation-vocabulary.md`](operation-vocabulary.md).

## The contract

Every source works with every target through the core. Nothing is written per pair of source and target: anything that would have to be is split into a half per source and a half per target ([ADR 0019](../adr/0019-shared-mappings-are-split-by-source-and-by-target.md)).

### A source provides

| Part | What it is | SQL Server |
|---|---|---|
| Extractor | Fills the catalog's neutral skeleton from a live server, files, or both, keeping all six promises: a read-only connection; never executes client code; never reads a row of a client table; records what it couldn't extract; a login that never holds the right to execute client code; and a check of its own permissions before reading, failing loudly if any is missing | Decided extractor, `VIEW DEFINITION` grants |
| Extension fields | Source-specific fields in a separate area on each object, never changing the skeleton's meaning | SQL Server extension area |
| Raw capture | Every metadata view it reads, captured wholesale, for the final validator | `sys.*` views, system tables on 2000 |
| Name resolver | The source's own name-resolution rules | |
| Parsers | One per version or compatibility level | ScriptDom, `TSql80Parser` to `TSql180Parser` |
| Lowering | Source constructs into vocabulary operations with explicit meanings, adding operations where none fits | |
| Secret patterns | Fixed patterns for likely secrets in its source text | Linked-server logins, connection strings |
| Version policy | Supported versions, the sandbox for each, the parser for each, what is verified only on a remote tier | [ADR 0015](../adr/0015-old-compatibility-levels-reconcile-only-on-a-guarded-test-server.md) |
| Error codes | Maps the core's neutral error classes to its own codes, messages and severities | SQL Server error numbers |
| Source half of the differences catalogue | How its constructs behave, mapped onto vocabulary meanings; feeds lowering and the edge-case pools | Re-filed from the T-SQL/Spark catalogue |
| Source half of the type mapping | Its types to vocabulary types | |
| Runner | Builds a scratch database with the client's settings, invokes each artefact type, reports session values and the execution window | |
| Tracing | An instrumented copy, a checkpoint transport that survives rollback and abort, and statement coverage of the uninstrumented original | Extended Events, user events |
| Two clients | Each returns a complete observation after an error; one for the testing module, one for the final validator, sharing no code | SqlClient, mssql-jdbc; FreeTDS and jTDS as fallbacks |
| Server row counts | Rows per statement from the server, to cross-check the clients | Extended Events |
| Validator path | The validator's own way to rebuild the database from the raw capture and run the original | |
| Lesson vocabulary | Its language's keywords and built-ins | T-SQL list |
| Free tools | Free and open source wherever possible, or recorded exceptions ([ADR 0017](../adr/0017-the-products-tools-are-free-and-open-source-where-possible.md)) | |

### A target provides

| Part | What it is | PySpark on Databricks |
|---|---|---|
| Generation | Vocabulary operations into target code, with its idioms | |
| Unsupported list | Each operation it doesn't handle, shown in the report as manual work | Full-text search |
| Target half of the type mapping | Vocabulary types to target types, with published representation rules | Stated in the delivered project |
| Target half of the differences catalogue | Where its built-ins differ from vocabulary meanings; feeds the support library, tolerances and mutation | Re-filed from the T-SQL/Spark catalogue |
| Error classes | Maps its runtime's failures to the core's neutral error classes | Spark exceptions to classes |
| Support library | Implements vocabulary operations its built-ins don't, packaged and vendored per project under versioned names ([ADR 0018](../adr/0018-the-support-library-ships-as-source-vendored-per-project.md)) | A wheel, `migrt_spark_v…` |
| Entry-point manifest | How to invoke each delivered artefact and read its results | |
| Code checks | Layer 1 for its language | Parse, lint, import allow-list |
| Expression extraction | How a linked expression's span is cut out of delivered code | |
| Mutation | Operators applied to its code, and the helper-to-built-in mapping | Python AST |
| Tracer | Checkpoints from delivered code with no markers, or, without a runtime hook, from an instrumented test copy that is never shipped | `sys.monitoring` |
| Runners | A sandbox runner, an optional remote-tier runner, and a separate validator runner | Local Spark, Databricks workspace |
| Second renderer | Optional, for the same-engine check | Spark SQL |
| Lesson vocabulary | Its language's keywords and built-ins, plus its support library's API | |
| Free tools | As for a source | |

### Both declare

- **The vocabulary version** they implement.
- **The operations, types and node kinds they add.** These are additions only, never a change to what an existing entry means.
- **For a target, what it doesn't support.** A target that cannot handle an operation, or cannot represent a type, declares it unsupported. Anything using it is manual work on that target, and the report says so. (The rule for types was added on 18 October 2026, in [Independent review of the assembled spec](https://github.com/Bach-To-The-Future/data-migration/issues/28).)

## Oracle PL/SQL as a second source

Everything fits as an addition to the vocabulary. No existing meaning changes.

| Area | Oracle construct | Fits as |
|---|---|---|
| Types | Records, `%ROWTYPE` | New type `record(fields)` |
| | Associative arrays, nested tables, VARRAYs | New type `collection(kind, key, element)` |
| | `NUMBER` (decimal with floating scale) | New type, with Oracle's arithmetic rules |
| | `INTERVAL YEAR TO MONTH`, `INTERVAL DAY TO SECOND` | New interval types |
| | `TIMESTAMP WITH TIME ZONE` with region names | New type: the existing one keeps only an offset |
| | `DATE` (with time, to the second) | `local_timestamp` with second rounding |
| | `ROWID` | New type `row_locator`, likely unsupported on targets |
| | `CLOB`, `BLOB`, `RAW`, `BINARY_DOUBLE` | Existing `string`, `binary`, `float` |
| Structure | Packages: members, initialisation, state that lasts for the session | New node kind; package variables as a new declaration; the catalog already allows members |
| | Overloading | Resolved by the name resolver to catalog identities |
| | Row-level and `BEFORE` triggers, `:NEW` / `:OLD`, compound triggers | Trigger body parameterised by timing and level; new operations to read and set new and old row values |
| Procedural | `SELECT INTO` raising `NO_DATA_FOUND` / `TOO_MANY_ROWS` | New `assign_exactly_one_row` (the existing `assign_from_single_value` gives null for no row) |
| | `SQL%ROWCOUNT` (reset only by SQL statements) | New operation, distinct from `rows_affected`, which every statement resets |
| | Collection methods, `BULK COLLECT`, `FORALL … SAVE EXCEPTIONS` | New operations |
| | Named exceptions, `RAISE_APPLICATION_ERROR`, `SQLCODE`, `SQLERRM`, `WHEN OTHERS` | New `raise_exception(code, message)` and error class tests; `try_catch` and `rethrow` reused |
| | Statement-level atomicity of a top-level call | Expressed with `save_point`, `try_catch` and `rollback_to_save_point` at lowering |
| | `PRAGMA AUTONOMOUS_TRANSACTION` | New `autonomous_block` |
| | Implicit commit around DDL | Lowering emits explicit `commit_transaction` |
| Query | `CONNECT BY`, `PRIOR`, `LEVEL`, `SYS_CONNECT_BY_PATH`, `ORDER SIBLINGS BY` | New `hierarchical_query`, keeping the depth-first order |
| | `(+)` outer joins, `DECODE`, sequences, `MERGE`, analytic functions, `PIVOT`, `AS OF` | Existing operations (`DECODE` is `case` with null-as-value comparison) |
| | `ROWNUM` | `limit` before sorting, or `row_number` over unordered input |
| | Empty string is null | New operation applied at lowering wherever a string value is produced |
| Results | Ref cursors as output parameters, `DBMS_SQL.RETURN_RESULT` | `cursor_handle` output, `emit_result_set` |
| Recognised only | Wrapped (obfuscated) source, the `MODEL` clause, DDL through `EXECUTE IMMEDIATE` | Manual work |

**The rest of the contract, for Oracle:**

- **Extraction.** The extractor reads the `ALL_` / `DBA_` dictionary views with a read-only dictionary grant, and source text from `ALL_SOURCE`.
- **Parsing.** The grammars-v4 `PlSql` grammar or sqlfluff's `oracle` dialect.
- **Clients.** `python-oracledb` in thin mode, and Oracle's JDBC driver: free but not open source, a recorded exception.
- **Tracing.** Coverage from `DBMS_PLSQL_CODE_COVERAGE`. Checkpoints carried by autonomous transactions, which survive a rollback. Per-statement row counts from SQL trace.
- **The sandbox.** Oracle Database Free (23ai), a platform exception. Clients on 19c or earlier are reconciled on a guarded source-side remote tier, unless a version-differences catalogue shows that 23ai is faithful for their constructs.

## Snowflake SQL as a second target

Everything fits as a target responsibility. No core change beyond those [ADR 0019](../adr/0019-shared-mappings-are-split-by-source-and-by-target.md) records.

| Area | Snowflake | The target's answer |
|---|---|---|
| Generated code | Snowflake Scripting procedures and SQL | Generation with its own idioms |
| Constraints | Only `NOT NULL` is enforced | The write path checks keys, uniqueness and foreign keys itself, inside the transaction |
| Triggers | None | Emulated by the write path after each write |
| Identity values | Identity and sequences may leave gaps | Counters in a state table, as on Databricks |
| Transactions | Explicit transactions, but no savepoints, and DDL commits | Savepoint operations declared unsupported or emulated, per operation |
| Errors | `EXCEPTION` blocks, `SQLCODE` | Its error codes mapped to error classes. Continuing after an error (ADR 0004) uses one handler block per statement |
| Result sets | A procedure returns one table | Several result sets packaged in a convention the entry-point manifest describes |
| Support library | No Python imports in SQL | A schema of SQL functions and procedures, vendored per project under a versioned schema name |
| Collation | Collation specifiers, not honoured by every function | Helpers, as on Spark |
| Six features | Time travel, `WITH RECURSIVE`, `CONNECT BY`, JSON functions, limited XML functions, `SEARCH` | Point-in-time reads from history tables, not time travel; recursion native; JSON and XML through helpers; full-text search unsupported |
| Sandbox | No local engine | A Snowflake account as the remote tier, where layers 2–6 and the final validator run. Only layer 1 runs locally |
| Tracer | No runtime hook | An instrumented test copy, with checkpoints in event tables (which survive rollback), never shipped |
| Clients | Python connector and JDBC (Apache 2.0) | One for the testing module, one for the validator |
| Mutation | SQL | Operators applied to the parsed SQL |
| Static check | No local compiler | Parsing locally; compiling in the account |

## The cross pairings

Added on 18 October 2026, in [Independent review of the assembled spec](https://github.com/Bach-To-The-Future/data-migration/issues/28). The walks above check Oracle against the vocabulary and Snowflake against SQL Server's constructs. These two check the pairings that actually test "only additions to the core": a new source on the existing target, and the existing source on a new target. Paper only.

### Oracle PL/SQL to PySpark

What the PySpark target does with each addition Oracle makes. Nothing changes in the core; where PySpark cannot represent something, it declares it unsupported.

| Oracle addition | PySpark target |
|---|---|
| `record(fields)` | A struct value; a struct column inside a DataFrame |
| `collection(kind, key, element)` | Held in session state as a typed Python collection in driver memory, through support-library helpers for get, set, delete, count, exists and iterate. `BULK COLLECT` streams rows into it, flagged for driver memory like a faithful cursor |
| `NUMBER(p, s)` with declared precision and scale | `DECIMAL(p, s)` |
| `NUMBER` with no declared scale (floating scale) | Declared unsupported: Spark's decimal has a fixed scale, so values could be cut. Anything touching it is manual, unless lowering proves a precision and scale that holds every value |
| Interval types | Spark's ANSI interval types (year-month and day-time) |
| Timestamp with a named time zone | `TIMESTAMP` plus a companion column holding the zone name, listed as a target-table requirement, as for SQL Server's `datetimeoffset` |
| `row_locator` (`ROWID`) | Declared unsupported: manual |
| Packages and package state | Package variables live in session state, keyed by package, initialised on first use in a run, as package state is per session |
| `assign_exactly_one_row`, `SQL%ROWCOUNT` | Session-state helpers: the first raises the error classes `no_data_found` and `too_many_rows`; the second is a counter that only SQL statements reset, beside `rows_affected` |
| `FORALL … SAVE EXCEPTIONS` | A helper that runs each write through the write wrapper, collects the failures and raises them together at the end |
| `raise_exception(code, message)` | An exception carrying the error class and the source's code, mapped by the source's half of the error mapping |
| `autonomous_block` | Its writes are kept out of the enclosing transaction's restore. If it writes a table the enclosing transaction also writes, compensating restore cannot separate them, and the block is manual |
| `hierarchical_query` (`CONNECT BY`) | An iterative helper, like recursive common table expressions, keeping depth-first sibling order, `LEVEL`, `PRIOR` and cycle detection |
| Empty string as null | Applied by lowering wherever a string value is produced; the target sees ordinary `null` |
| Row-level and `BEFORE` triggers reading and setting new and old values | The write wrapper calls the trigger function per row before the write and writes the rows it returns, flagged for performance |

### SQL Server to Snowflake

How the Snowflake target carries what SQL Server's session holds and PySpark keeps in session state.

| SQL Server behaviour | Snowflake target |
|---|---|
| Session state between procedures (`@@ROWCOUNT`, `@@ERROR`, the last identity value, the error being handled) | Converted procedures run with caller's rights, so they share the caller's session. Session state lives in a session-scoped temporary table owned by the support library, read and written through its procedures |
| Temp tables and their visibility by call depth | Snowflake temporary tables, named with the call depth by the support library so a callee can shadow a caller's `#name` and drop its own on return |
| `@@ROWCOUNT` | `SQLROWCOUNT` after each statement, copied into session state where the code reads it |
| Identity values and row versions | Counters in the state table, through the write path |
| Transactions | Explicit transactions; savepoints declared unsupported or emulated per operation; data-definition statements commit, so a scratch table is never created inside an open transaction |
| Several result sets from one procedure | Packaged as one result in the convention the entry-point manifest describes |
| Errors | Snowflake's error codes mapped to error classes, and error classes to SQL Server's codes by the source's half |
| Case-insensitive comparison | Collation specifiers where every function involved honours them, helpers otherwise, as on Spark |

## The six target-dependent features on Databricks

The owner decided each one on 17 October 2026. Every helper is tested against SQL Server like any other.

| Feature | Corpus | Decision |
|---|---|---|
| Point-in-time reads | 9 | Supported. A query over the current and history tables with the period predicates, never Delta time travel |
| Recursive queries | 4 | Supported through an iterative helper, keeping `MAXRECURSION` (100 by default, error 530 when exceeded) |
| JSON output | 5 | Supported through a helper reproducing `FOR JSON` formatting, omitted nulls and escaping |
| XML values | 8 | Supported for a defined XPath subset (`.value()`, `.exist()`) through a helper; anything outside it is manual |
| XML shredding | 6 | Supported for the same subset (`.nodes()`, `OPENXML`); anything outside it is manual |
| Full-text search | 1 | Unsupported: manual work |
