# Engine

The script migration tool's engine, `migrt`: the core, the SQL Server source and the PySpark target, built from the [v1 architecture spec](../docs/spec/v1-architecture-spec.md). Its library interface is [`migrt.engine`](src/migrt/engine.py); the command line ([`migrt.cli`](src/migrt/cli.py)) is a thin front end over it.

**State:** the skeleton of [issue #36](https://github.com/Bach-To-The-Future/data-migration/issues/36), with full live extraction for SQL Server 2017 and later ([issue #39](https://github.com/Bach-To-The-Future/data-migration/issues/39)), extraction from folders with inventories of SSIS packages and SQL Agent jobs ([issue #40](https://github.com/Bach-To-The-Future/data-migration/issues/40)), the lowering of queries and expressions of views and functions ([issue #42](https://github.com/Bach-To-The-Future/data-migration/issues/42)), the lowering of procedural code: control flow, variables, temp tables and table variables, with explicit scopes, in procedures, triggers, batches and function bodies ([issue #43](https://github.com/Bach-To-The-Future/data-migration/issues/43)), the rest of it, cursors, transactions and savepoints, `TRY`/`CATCH`, errors, messages, row counts and identity ([issue #85](https://github.com/Bach-To-The-Future/data-migration/issues/85)), and generation with the delivered project ([issue #44](https://github.com/Bach-To-The-Future/data-migration/issues/44)). Views, functions, procedures, triggers and query batches run end to end as a preview run: the extractor's version check and permission self-check, extraction of every catalog level with its raw capture, references and dependency cross-check, an immutable snapshot in a per-engagement Postgres control database with its name adjustments, lowering into the intermediate representation, the checker, detection of what the target cannot translate, generation, layer-1 code checks, the delivered project, and `report-data.json`. Later tickets widen each layer.

## Run it

The engine runs in the sandbox toolbox, which carries .NET, ScriptDom, Microsoft.Data.SqlClient and the Python dependencies. Build the sandbox first; that also starts Postgres and grants the extractor's read-only login by Annex F's grant set:

```
python corpus/build.py
```

Then, for a preview run of one view:

```
docker compose -f sandbox/docker-compose.yml --profile tools run --rm tools \
    python -m migrt preview --engagement retail_demo --database RetailSample --sandbox sqlserver \
    --artefact RetailSample.sales.vOrderTotals --out out/retail_demo
```

It writes the delivered project under `out/retail_demo/` (`converted/RetailSample/sales/views/vOrderTotals.py` among it) and `out/retail_demo/report/report-data.json`, and prints the run's result as JSON. `--artefact` takes views, functions, procedures and triggers as `DB.schema.name`, and a query batch from a folder as `batch:DB/path/file.sql`. Every artefact in a preview run is "Not reconciled": nothing in it is converted. On SQL Server 2017, where `CURRENT_TIMEZONE()` fails, pass the server's time zone with `--time-zone`.

Folders of exported files go beside the server with `--folder DB=PATH`, or stand alone with `--no-server`, which needs each database's default collation and compatibility level, the SQL Server release and the time zone:

```
docker compose -f sandbox/docker-compose.yml --profile tools run --rm tools \
    python -m migrt preview --engagement retail_files --no-server \
    --folder RetailSample=corpus/sample/database --folder RetailSample=corpus/sample/artefacts \
    --collation RetailSample=SQL_Latin1_General_CP1_CI_AS --compatibility-level RetailSample=160 \
    --sql-server-version 2022 --time-zone UTC \
    --artefact RetailSample.sales.vOrderTotals --out out/retail_files
```

## Extraction

The extractor reads each metadata view only as `SELECT * FROM <view>` and builds every fact from those rows, so the raw capture holds exactly what the facts came from ([ADR 0024](../docs/adr/0024-the-extractor-reads-metadata-views-only-whole.md)). The views are listed in [`extractor.py`](src/migrt/sources/sqlserver/extractor.py). References are worked out by the parser and cross-checked against `sys.sql_expression_dependencies` where the optional grant is held; the report data's `extraction` section lists every disagreement, every check that was skipped or not run, the marked secrets, and the self-check's exemptions. Procedure result shapes are not extracted: they need `EXECUTE`, so they are described in the sandbox ([`shapes.py`](src/migrt/sources/sqlserver/shapes.py), [issue #41](https://github.com/Bach-To-The-Future/data-migration/issues/41)), or taken from an administrator's export, a CSV in a folder written by the script in Annex F ([`shape_export.py`](src/migrt/sources/sqlserver/shape_export.py)). Give the sandbox with `--sandbox HOST[:PORT]` (login `sa` and password from `$MSSQL_SA_PASSWORD` unless `--sandbox-login` and `--sandbox-password-env` say otherwise), or as `engine.Sandbox` in the library. Each procedure then has a `result_shape` fact, or `result_shape_unavailable` holding why it could not be described; both are summarised in the report data's `extraction.result_shapes`. Without a sandbox or an export, every procedure says so. Views and inline functions read from folders are described the same way.

Folders ([`folders.py`](src/migrt/sources/sqlserver/folders.py)) give query batches and SSIS packages beside a server, and without one also the data definition, replayed `CREATE` before `ALTER` in file-name order, and job scripts. Beside a server, the server is the only source of its databases' objects ([ADR 0025](../docs/adr/0025-a-live-server-is-the-only-source-of-its-databases-objects.md)). Without one, every fact a server would have supplied is assumed, as a fact with assumed provenance or as an entry in its object's `assumed_values`, and the report data's `extraction` section has the completeness summary, every column whose collation was assumed, and every file and statement that was not read, not replayed or superseded. SSIS packages ([`ssis.py`](src/migrt/sources/sqlserver/ssis.py)) and SQL Agent jobs ([`jobs.py`](src/migrt/sources/sqlserver/jobs.py)) are inventoried, with their references to catalog objects worked out by the parser, and listed in `recognised_artefacts` as manual work, every marked secret masked.

## Layout

```
src/migrt/
  engine.py            the library interface
  cli.py               the command line
  core/                shared by every source and target; imports neither
    catalog.py         facts with provenance, snapshots, the query interface
    control_db.py      one Postgres database per engagement; snapshots immutable
    ir.py              the intermediate representation
    checker.py         verifies the representation
    vocabulary.py      the operation vocabulary (Annex A), with its JSON copy
    differences.py     the silent-differences catalogue: the one interface to both halves
    error_classes.py   the neutral error classes (error-classes.json): what a target maps failures to and a source maps to codes
    secrets.py         marked secrets, masked by named settings wherever text leaves the catalog
    name_adjustments.py  extending the recorded name adjustments by a target's rules
    report.py          the report data
  sources/sqlserver/   connection (SqlClient), extractor, folders (files: the replay, batches),
                       shapes (result shapes, described in the sandbox), shape_export (the administrator's export),
                       ssis and jobs (the recognised artefacts' inventories), references (the
                       parser's) and resolution (resolving them), secrets (the patterns), parser
                       (ScriptDom), names (the resolver), types, lowering, differences (the
                       source half, Annex C, as data), error_mapping (the error classes to SQL Server's numbers, messages,
                       severities and reach, as data), error_comparisons (comparisons against numbers it does not cover),
                       transactions, cursors and errors (the lowering of transactions and savepoints, of cursors, and of
                       TRY/CATCH, RAISERROR, THROW, PRINT and the reads of the session's state; each extends the one before,
                       starting from procedural) and statement_effects (what each statement leaves in @@ROWCOUNT and @@ERROR)
  targets/pyspark/     generator, draft (the file so far; giving up a construct undoes it completely), untranslatable (detection before generation), layer1, project (the delivered
                       project), types (the target half of the type mapping, with the representation rules),
                       names (Unity Catalog and module name rules), differences (the target half, Annex D, as data)
src/migrt_spark/       the PySpark target's support library, shipped in every delivered project; imports nothing from migrt
  session.py           session state and the write wrappers
  transactions.py      transaction() as compensating restore, savepoints
  constraints.py       not null, check, key, unique and foreign-key enforcement
  state.py             the state table: counters, leases, transaction start versions, scratch registrations
  recovery.py          recovery on start and the recovery command
  errors.py            TsqlError and the library's named errors
  target_errors.py     Spark and Delta failures to error classes (spark-failures.json)
  source_errors.py     applies the source's half of the error mapping (the project's error-mapping.json)
  raiserror.py         the text of RAISERROR and THROW
  tables.py, settings.py, config.py, deltaops.py
tests/                 run with sandbox/engine-tests.sh
```

## Lowering

Views, inline and multi-statement table-valued functions, scalar functions, procedures, triggers and batch scripts lower into the core's intermediate representation ([`lowering.py`](src/migrt/sources/sqlserver/lowering.py); the conventions are in the docstrings of [`queries.py`](src/migrt/sources/sqlserver/queries.py), [`expressions.py`](src/migrt/sources/sqlserver/expressions.py) and [`procedural.py`](src/migrt/sources/sqlserver/procedural.py)). A query is a logical plan that keeps the source's names and the order of its steps; every expression has its T-SQL type from the catalog, with the conversions SQL Server's data type precedence makes written out as `implicit_convert`; every string comparison names its comparison rule from collation precedence (two different implicit collations are a conflict and stay an opaque fragment). Table, join and query hints are kept on their nodes and reported as not applicable on the target; comments annotate the nearest node; a literal inside a marked secret lowers to its named setting.

What lowering cannot express is an opaque fragment with its reason: the whole query, the one select item, or the one statement. Procedural code ([`procedural.py`](src/migrt/sources/sqlserver/procedural.py), [issue #43](https://github.com/Bach-To-The-Future/data-migration/issues/43)) lowers variables (`DECLARE`, `SET`, `SELECT @v =`), table variables, temp tables (`CREATE TABLE #t`, `SELECT INTO #t`, `DROP TABLE`), control flow (`IF`, `WHILE`, `BREAK`, `CONTINUE`, `GOTO` and labels, `RETURN`), the statements that read and write rows (a `SELECT` result set, `INSERT`, `UPDATE`, `DELETE`, also under a common table expression), `EXEC` of a catalog procedure and `SET NOCOUNT` and `SET XACT_ABORT`. A trigger reads `inserted` and `deleted` as `read_changed_rows`, and a batch script is one `batch` per `GO`. Every statement carries its scope ([`core/scopes.py`](src/migrt/core/scopes.py)): the parameters, variables, table variables and temp tables in force before it, and the last scope barrier before it. After a scope barrier the entries may be stale, so every node that depends on one carries `scope_may_be_stale` (the barrier's position): generation handles it or attaches a caveat, and the report shows it at the construct. The checker derives the scopes and the flags again and refuses a difference, a name outside its scope and a jump to no label. A statement kept as written either records what it touches (a `RAISERROR` of a message number, an `INSERT` with an `OUTPUT` clause, a `FETCH` into a variable that is not in scope) or is a scope barrier (a transaction statement, a cursor declaration, an unresolved `EXEC`). Dynamic SQL has its own node kind.

Cursors, transactions, errors and messages ([issue #85](https://github.com/Bach-To-The-Future/data-migration/issues/85)) lower as the T-SQL says them, never raised to a higher form. A cursor is `declare_cursor` (its query, the columns and types it returns, its scope, kind and options; a cursor with neither `LOCAL` nor `GLOBAL` takes the database's default, which the catalog does not hold, recorded as an assumption), `open_cursor`, `fetch_cursor` (the direction, the variables, and the type of each column and each variable), `close_cursor` and `deallocate_cursor`, and `UPDATE`/`DELETE ... WHERE CURRENT OF` is `write_current_row`; a cursor whose query lowering cannot express stays a cursor, with its query kept as written. Transactions are statements, not blocks (`begin_transaction`, `commit_transaction`, `rollback_transaction`, `save_point`, `rollback_to_save_point`, `set_isolation_level`), because a transaction a procedure opens and does not close is still open in its caller. `TRY`/`CATCH` is `try_catch` with its `body` and its `handler`, `RAISERROR` and `THROW` are `raise_error` (a bare `THROW` is `rethrow`), `PRINT` is `emit_message`, and `@@ROWCOUNT`, `@@ERROR`, `@@TRANCOUNT`, `XACT_STATE()`, `@@FETCH_STATUS`, the six `ERROR_*()` functions, `SCOPE_IDENTITY()`, `@@IDENTITY` and `IDENT_CURRENT()` are reads of the session's state (`rows_affected`, `last_statement_error`, `transaction_depth`, `transaction_state`, `fetch_status`, `error_field`, `last_identity`); `SET IDENTITY_INSERT` is a session option naming its table. What `@@ROWCOUNT` and `@@ERROR` hold after each kind of statement is [`statement_effects`](src/migrt/sources/sqlserver/statement_effects.py), decided against SQL Server (`tests/test_statement_effects_sqlserver.py`): `DECLARE @c CURSOR` keeps the row count and resets the error number, and `BREAK` keeps both. What stays opaque, with its reason: `RAISERROR` of a message number (the catalog holds no messages), a cursor in a variable or named `GLOBAL`, a rollback to a name that is neither a savepoint nor a transaction of the body, `BEGIN DISTRIBUTED TRANSACTION`, `WITH MARK` and delayed durability.

The scope says more now ([`core/scopes.py`](src/migrt/core/scopes.py)): the cursors that may be declared and open, and the open transactions as a range of levels with the `BEGIN` statements that may still be open and the savepoints that may be set. A rollback undoes the creation of the temp tables made inside the transaction and brings back the ones dropped inside it (both are transactional in SQL Server), never undoes a table variable, and a rollback to a savepoint does the same for what came after it. A TRY block is no longer a scope barrier: its handler starts in what may be in scope after any statement of the block. The checker verifies that every cursor a statement names is in scope, that a fetch names variables in scope and as many as the cursor has columns, and that a rollback to a savepoint names one that may be set; `tests/test_lowering_corpus.py` runs it over every procedure, trigger, function and sample script of the three corpus databases. Generation of the new nodes is [issue #50](https://github.com/Bach-To-The-Future/data-migration/issues/50): until then each is a manual item (see Generation and the delivered project), never wrong code.

## Generation and the delivered project

[`generator.py`](src/migrt/targets/pyspark/generator.py) generates PySpark from the representation, deterministically. A view and an inline table-valued function are functions of session state `ss` (and their parameters) returning a DataFrame; a scalar function whose body is one `RETURN <expression>` is a function building a column expression; ANSI mode is pinned in every function. A multi-statement table function, a scalar function with control flow, a procedure, a trigger and a batch script are procedural generation ([issue #50](https://github.com/Bach-To-The-Future/data-migration/issues/50)): the file holds the signature (a trigger takes `inserted` and `deleted`; a batch script has one function per `GO` batch and a list `BATCHES` giving their order) and one manual item for each statement, and the function raises when called. A plan step or a column the target cannot generate exactly is a manual item too: the column fails loudly where it is used, or the function raises when called. A dependency between generated artefacts is an import: a view reading a generated view, or calling a generated scalar function, imports its module; one that is not generated in the project is manual work, never a table read of a view.

A node kind is generated if the generator has a handler for it, declared by decorating the code that generates it; `handled_kinds()` lists them, so adding a handler is the only edit a kind needs. Untranslatable constructs are detected before generation ([`untranslatable.py`](src/migrt/targets/pyspark/untranslatable.py)): every node the target has no handler for, every opaque fragment, dynamic SQL, schema and permission change, every node of an unsupported type, and every node carrying `scope_may_be_stale`, which is never generated as if its scope were certain. Generation must account for each finding with a manual item over it or a node above it; one it finds nowhere in the file is `DroppedConstruct`, an error. A literal inside a marked secret is read from its named setting: `ss.setting("<identity>.secret_<n>")`, where the setting holds the whole literal's value (a literal holding several marked secrets is manual work).

Each linked expression is emitted as one contiguous, self-contained span of code, recorded with its position. Layer 1 checks that each span parses as one expression and reads nothing but the module's imports and its function's parameters, never a local variable, and that spans nest or stand apart; the testing module can then evaluate any expression alone.

The delivered project ([`project.py`](src/migrt/targets/pyspark/project.py)) holds `converted/<Database>/<schema>/<kind>/<Name>.py` (a query batch under `converted/<Database>/batches/`, named from its file's path), a package in every folder, `manifest.json` (one entry point per artefact: module, functions, how to call it, parameters, result, columns, status, manual items), `settings.json` (the semantic settings with their origin and a fingerprint, the runtime identity values, and the named settings for marked secrets, to be filled in), `representation-rules.md` (the target half of the type mapping, [`types.py`](src/migrt/targets/pyspark/types.py)) and `README.md`. Not carried yet: the vendored support library with its licence, the state table's definition (`migrt_spark.state_table_ddl`) and the commands ([issue #49](https://github.com/Bach-To-The-Future/data-migration/issues/49)). DATEFIRST, DATEFORMAT and the language are not read by the extractor: the settings file holds SQL Server's defaults for us_english, marked assumed, and the report says so.

## The support library

[`migrt_spark`](src/migrt_spark/) is the PySpark target's support library ([issue #45](https://github.com/Bach-To-The-Future/data-migration/issues/45) built its core and [issue #46](https://github.com/Bach-To-The-Future/data-migration/issues/46) its errors; packaging and delivery are [issue #49](https://github.com/Bach-To-The-Future/data-migration/issues/49)). `SessionState.open(spark, job=, run=, state_table=, tables=, settings=, errors=)` starts a run: it recovers what earlier runs left unfinished, then starts the lease heartbeat. Generated code writes only through it: `insert`, `insert_values`, `update`, `delete`, inside `transaction()` blocks where the source had a transaction. A table is described by a `TableSpec` (columns, identity and row-version columns, keys, checks, foreign keys, triggers) that the delivered project will carry for each table it writes; building one from the catalog is part of procedural generation. The behaviours are tested against SQL Server: `tests/test_support_*.py` run the same statements on both sides and compare rows, row counts, identity values and the errors raised (number, severity, state and text).

A transaction is also available one statement at a time, for code that is not block-shaped: `ss.begin_transaction()`, `commit_transaction()`, `rollback_transaction()`, `save_transaction(name)`, `rollback_to_save_point(name)` and `xact_state()`. A transaction a procedure opens and does not close is still open in its caller, which reads `ss.trancount` and commits or rolls it back. A procedure called with EXEC (`ss.scope(name, procedure_call=True)`) that ends with a different number of transactions open than it was called with raises error 266, which ends only that call, as in SQL Server.

What a write does, in order: take the table's write lease (once per run), note its Delta version with the open transaction, check not null, check, key and foreign-key constraints, write, run foreign-key actions, then triggers. A failure anywhere in a statement that writes more than one table undoes the statement. Known limits, each loud rather than silent: string comparisons for keys follow the collations the library knows (case-sensitive, case-insensitive, binary, accent-sensitive; any other raises `UnsupportedCollation`); an `UPDATE` on a table with a trigger or a row version and no primary key is `ManualWrite`; `MERGE`, the length and range rules of the representation rules, and a trigger's rows read from Delta's change data feed are not built yet.

## Errors

Errors follow [ADR 0019](../docs/adr/0019-shared-mappings-are-split-by-source-and-by-target.md): the core lists neutral **error classes** ([`error-classes.json`](src/migrt/core/error-classes.json), each with the details it takes); the PySpark target maps Spark and Delta failures, by their error condition, to a class and its details ([`spark-failures.json`](src/migrt_spark/spark-failures.json), inside the library, so the library version identifies it); the SQL Server source maps each class to a number, severity, state and message, and to **how far SQL Server lets the error reach** ([`error-mapping.json`](src/migrt/sources/sqlserver/error-mapping.json)). The delivered project carries the source's half as `error-mapping.json`, and `SessionState.open(..., errors=...)` reads it, so the library holds the target half only and one interpreter applies any source's file. A class a source has no entry for, or a variant its entry does not cover, and a failure no target rule covers, take the source's **reserved number** (2147483600), with the reason in the message; the session lists them in `ss.unmapped_errors`, and a comparison against a number the mapping does not cover is flagged at the artefact in the report (`flags`, kind `unmapped_error_number`).

Every entry was decided against SQL Server in the sandbox (`tests/test_error_mapping_sqlserver.py`, one case per error in `tests/error_cases.py`), and the two halves are tested together against it (`tests/test_error_mapping.py`): a Spark or Delta failure goes through both and must give the number, severity, state and text SQL Server reports for the same operation. Three things found there:

- **An error ends the statement, the scope or the batch.** `ends` is `statement` (the next statement runs: constraint violations, divide by zero, `RAISERROR`), `scope` (the rest of the procedure does not run and its caller goes on: a name that does not resolve when the statement runs, 207 and 208) or `batch` (nothing after it runs: `THROW`, a failed conversion, 2714). The spec and ADR 0004 spoke of two; at the top level a scope-ending error is a batch-ending one.
- **Some errors are not caught beside the statement.** `catchable_at_same_level` is false for a name that does not resolve (finding 7.6): a `TRY` at the same call depth does not see it, and the `TRY` of a caller does. Call depths are `ss.scope(name)`.
- **A message may be a stand-in.** Spark does not say a failed conversion was of a `varchar` or an `nvarchar`, and Delta folds a constraint name to lower case. The details the generated code knows (the types, the catalog's spelling) are added by the caller; without them the error's `exact_message` is false and its text says less. Number, severity and state are never stand-ins.

Not covered yet, each by an unmapped failure, never by another error's number: the smaller integer overflows (220), string truncation (2628, and 8152 before SQL Server 2019), conversion to `float`, and the many errors Spark has no counterpart for. A class is added by the ticket that builds the behaviour that raises it, with a case in `tests/error_cases.py`.

**In generated code.** `ss.raise_error(text, severity, state, *args)` is `RAISERROR` (at severity 10 or below a message in `ss.messages` and `ss.message_records`, never an exception; formatting as SQL Server does, `%d %i %u %o %x %X %s`, flags, width, precision and `*`), `ss.throw(number, text, state)` is `THROW` and `ss.rethrow()` is `THROW;`. `ERROR_NUMBER()` and the rest are `ss.error_number()`, `error_severity()`, `error_state()`, `error_message()`, `error_line()` and `error_procedure()`; the last two are what generated code said (`ss.statement_line = n` before a statement, `ss.scope(name)`), and a loud `ErrorFieldUnknown` when it said nothing. `TRY`/`CATCH` is

```python
with ss.try_catch() as block:
    ...                                # BEGIN TRY
if block.error is not None:
    with ss.handling(block.error):     # BEGIN CATCH
        ...
```

The block holds an error a `CATCH` would have caught, and raises again one it would not have: a name that does not resolve at the same call depth, and anything that is not an error of the source at all (a Python bug is never swallowed). A procedure that reads `@@ERROR` puts each statement in `with ss.guarded():`: a failure records its number in `ss.at_error` and the procedure goes on, stops, or ends everything, as the error's `ends` says; inside a `try_catch` at the same call depth, or with `ss.set_xact_abort(True)`, the error is raised instead. A scope-ending error inside a transaction leaves the transaction open, as SQL Server does with `XACT_ABORT` off: the `ScopeAborted` that ends the procedure passes through a `transaction()` block without a rollback, and the caller goes on with `@@TRANCOUNT` unchanged and may commit or roll back (`tests/test_support_scope_abort_in_transaction.py` runs the same procedures on SQL Server and on the library, in both forms a transaction can take). With `XACT_ABORT` on the error ends the batch, and a guarded statement rolls the open transaction back before it raises. Known limits: a batch-ending error with `XACT_ABORT` off still rolls a `transaction()` block back where SQL Server leaves the transaction open; `xact_state()` is 1 or 0, never -1 (a transaction doomed inside a `TRY` by `XACT_ABORT` stays committable; both limits are [issue #113](https://github.com/Bach-To-The-Future/data-migration/issues/113), and a construct that depends on either is manual work until then); `RAISERROR ... WITH LOG` is `UnsupportedRaise`; severities 17 and 18 are raised like 16 but were not run against the oracle, whose client turns them into exceptions.

## Checks

```
sh sandbox/engine-tests.sh            # lint (pyflakes), then types (pyright), then tests (pytest)
sh sandbox/engine-tests.sh -k layer1  # arguments go to pytest
```

All three run in the toolbox; the tests run against the sandbox SQL Server and Postgres, including a case-sensitive scratch database that `tests/conftest.py` creates for the session and drops after it, through the engine's library interface, the PySpark target's layer-1 contract, and the control database for the immutability claim. Imports of .NET namespaces carry a targeted `pyright: ignore`, because pythonnet provides them only at run time.

CI runs the same script after a from-scratch corpus build, on every push to master, on pull requests to master, nightly and on demand ([`.github/workflows/sandbox.yml`](../.github/workflows/sandbox.yml)).

Tests that need no server, such as `tests/test_differences.py`, `test_secrets.py`, `test_name_adjustments.py`, `test_name_resolution.py` and `test_recognised_artefacts.py`, also run on the host for a quick loop: `cd engine && PYTHONPATH=src python -m pytest tests/test_differences.py`. The sandbox run above stays the gate.

**A refactor that must not change output** is checked with [`tools/generation_snapshot.py`](../tools/generation_snapshot.py), the standard check for it: capture everything generation produces for the three corpus databases before the change (each artefact's generated text, links and spans, manual items, assumptions, caveats, settings, layer-1 result and report entry, or the error it fails with), change the code, capture again, and compare byte for byte. The capture runs in the toolbox; the comparison needs only Python and names every file that differs, is missing, or exists on one side only (an empty capture is a failure). Capture twice on the same code first if in doubt: the two must be identical.

```
docker compose -f sandbox/docker-compose.yml --profile tools run --no-deps --rm -T tools \
    python tools/generation_snapshot.py capture .snapshots/before
# ... change the code, then capture .snapshots/after the same way ...
python tools/generation_snapshot.py compare .snapshots/before .snapshots/after
```

It complements the tests and does not replace them: the corpus is mostly procedures, so the expression handlers are covered by `tests/test_generation.py`. The comparison half has its own tests (`tests/test_generation_snapshot.py`).

A pre-commit hook lints staged engine files with pyflakes on the host, without Docker. Enable it once per clone:

```
git config core.hooksPath .githooks
```

Each half of the silent-differences catalogue is a JSON file in its package, checked cell by cell against Annexes C and D and the operation vocabulary by `tests/test_differences.py`. Change an annex and its half together; each half's version is the hash of its entries.

Review applies [`CODING_STANDARDS.md`](../CODING_STANDARDS.md).
