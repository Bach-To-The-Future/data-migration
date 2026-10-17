# Operation vocabulary, first draft

Generated from `operation-vocabulary.json` by `python corpus/tools/vocabulary_check.py`. Edit the JSON, not this file.

This is the first list of what the intermediate representation can say: its types, its operations and the node kinds that are not operations. It is a draft for the spec. The full vocabulary grows during the build.

## Rules it follows

1. Every operation has one meaning, whatever source it came from. Lowering resolves the source's behaviour, using catalog types and collations, into the operation that means exactly that.
2. The vocabulary grows by addition only. A new source or target may add an entry and may never change what an existing one means.
3. A target either handles an operation or declares it unsupported, which the report shows as manual work.
4. Operations are typed. Where the meaning depends on operand types, there is one operation per meaning (integer_divide and decimal_divide), not one operation with hidden behaviour.
5. Comparison of strings always names its comparison rule (case sensitivity, trailing-space handling), taken from the source's comparison rules at lowering (for SQL Server, its collation precedence).
6. The query part is a logical plan in our own words: kinds of step borrowed from Spark's plan concepts, each with the source's meaning.

## Size

- 18 types, 12 node kinds, 7 annotations.
- 177 operations, of which the hand conversion in `prototype/hand-conversion/` exercised 85. The other 92 are on paper only.

## How to read the tables

- **Comes from** lists T-SQL words that lower to the entry. An empty cell means the entry is chosen by meaning, not by spelling: `integer_divide` comes from `/` only when both operands are integers.
- **Artefacts** counts how many of the 132 corpus artefacts (34 sample, 98 public) appear to use the entry. It is pattern matching on source text, not parsing, so treat it as an index of where to look. An empty cell means it cannot be counted that way.
- **Hand** is ✓ where the hand conversion exercised the entry.

## Types

A type carries the behaviour that matters, not the source's name for it. The source's own type name is kept alongside for the report.

| Type | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `boolean` | True, false or null. | `BIT` | 8 | ✓ |
| `integer(bits, signed)` | A whole number of 8, 16, 32 or 64 bits. Going outside the range is an error, never a wrap. SQL Server's tinyint is integer(8, unsigned): 0 to 255. | `TINYINT`, `SMALLINT`, `INT`, `BIGINT` | 74 | ✓ |
| `decimal(precision, scale)` | An exact decimal number. Results of arithmetic take SQL Server's precision and scale rules. | `DECIMAL`, `NUMERIC` | 4 |  |
| `money(precision)` | An exact decimal with four places, precision 19 or 10, whose multiplication and division truncate to four places at every step. | `MONEY`, `SMALLMONEY` | 8 | ✓ |
| `float(bits)` | A binary floating-point number of 53 or 24 bits. | `FLOAT`, `REAL` | 0 |  |
| `date` | A calendar date with no time and no zone. | `DATE` | 13 | ✓ |
| `local_timestamp(rounding)` | A date and time with no zone. 'rounding' says how a value is stored: to 1/300 of a second (datetime), to the minute (smalldatetime), or to a number of fractional digits up to 7 (datetime2). | `DATETIME`, `SMALLDATETIME`, `DATETIME2` | 34 | ✓ |
| `timestamp_with_offset(digits)` | A date and time with its offset from UTC kept. | `DATETIMEOFFSET` | 0 |  |
| `time_of_day(digits)` | A time with no date. | `TIME` | 1 |  |
| `string(length, unit, fixed, collation)` | Text. 'length' is the maximum, or unlimited; 'unit' is bytes or UTF-16 units, as SQL Server counts; 'fixed' means shorter values are padded with spaces; 'collation' gives the comparison rule. | `CHAR`, `NCHAR`, `VARCHAR`, `NVARCHAR`, `TEXT`, `NTEXT`, `SYSNAME` | 58 | ✓ |
| `collation(case_sensitive, accent_sensitive, ignore_trailing_spaces, origin)` | How two strings compare and sort. 'origin' is explicit, column or default, which decides precedence and whether mixing two is an error. |  |  | ✓ |
| `binary(length, fixed)` | A sequence of bytes. | `BINARY`, `VARBINARY`, `IMAGE` | 3 |  |
| `guid` | A 16-byte identifier that sorts and compares in SQL Server's own byte order, which is not text order. | `UNIQUEIDENTIFIER` | 0 |  |
| `xml` | An XML document or fragment. | `XML` | 0 |  |
| `row_set(columns)` | A set of rows with named, typed columns: the type of a table variable, a table-valued parameter, a result set and every query step. | `TABLE` | 23 | ✓ |
| `cursor_handle` | A position in a set of rows being read one at a time. | `CURSOR` | 10 | ✓ |
| `unsupported_type(source_name)` | A source type with no neutral meaning yet: sql_variant, hierarchyid, spatial types, rowversion. Anything touching a value of this type is manual. | `SQL_VARIANT`, `HIERARCHYID`, `GEOGRAPHY`, `GEOMETRY`, `ROWVERSION`, `TIMESTAMP` | 2 |  |
| `unknown` | A type lowering could not establish, for example when a table shape is missing. Operations on it are flagged. |  |  |  |

## Node kinds that are not operations

| Node kind | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `artefact` | The outer record every artefact has: identity, artefact type, tier, dependencies, original source, status. Its body is one of the kinds below, or absent for a recognised type. | `CREATE` |  | ✓ |
| `procedure_body` | Parameters, a statement list, and what it gives back: a return code, output parameters and result sets in order. |  |  | ✓ |
| `view_body` | One query. |  |  | ✓ |
| `scalar_function_body` | Parameters, a statement list and a single returned value. |  |  | ✓ |
| `table_function_body` | Either one query (inline) or a statement list that fills a declared row set (multi-statement). |  |  |  |
| `trigger_body` | The table, the timing (after or instead of), the events, and a statement list that can read the inserted and deleted rows. | `TRIGGER` |  | ✓ |
| `batch_script_body` | An ordered list of batches. Variables do not survive from one batch to the next. | `USE` |  | ✓ |
| `opaque_fragment` | Source kept verbatim because lowering could not express it, with its position and the reason. It records what it reads and writes in scope where detectable, and is otherwise a scope barrier. |  |  |  |
| `dynamic_sql_execution` | Execution of SQL text built at run time. The expressions that build the text are lowered; the text itself is not. Always a scope barrier, always manual. | `EXECUTE(`, `EXEC(`, `SP_EXECUTESQL` | 15 |  |
| `schema_change` | A statement that creates, alters or drops a permanent object. Schema migration is out of scope, so it is recorded and manual. Temp tables are not schema changes. | `ALTER`, `DROP`, `INDEX`, `STATISTICS`, `SEQUENCE`, `SYNONYM`, `TYPE` | 10 |  |
| `external_reference` | A reference to an object outside the catalog: another database, a linked server, a system object. | `OPENQUERY`, `OPENROWSET` |  |  |
| `permission_change` | A statement that grants, revokes or denies a permission, or switches security context. Recorded and manual. | `GRANT`, `REVOKE`, `DENY`, `REVERT` | 0 |  |

## Annotations

Carried on nodes. None is an operation and none changes what a node means.

| Annotation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `comment` | A source comment, attached to the nearest node and carried to the generated code unchanged. Untrusted text. |  |  | ✓ |
| `query_hint` | A table hint or query option. Kept on its step and reported as not applicable on the target. | `OPTION`, `NOLOCK`, `READPAST`, `UPDLOCK`, `HOLDLOCK`, `TABLOCK`, `ROWLOCK` | 4 |  |
| `module_option` | An option on the artefact itself: the security context it runs under, schema binding, native compilation, encryption. Reported; none changes the conversion. | `SCHEMABINDING`, `NATIVE_COMPILATION`, `ENCRYPTION`, `EXECUTE AS` | 42 | ✓ |
| `stored_setting` | The ANSI_NULLS and QUOTED_IDENTIFIER settings stored with a routine, which lowering uses to choose between operations. | `ANSI_NULLS`, `QUOTED_IDENTIFIER` |  |  |
| `source_position` | Where a node came from in the original. Every node has one. |  |  | ✓ |
| `producer` | Who produced a node: deterministic code, or an LLM with prompt and model version. |  |  |  |
| `provenance` | Whether a fact a node relies on was read, parsed or assumed. |  |  |  |

## Procedural operations

### Declarations and scope

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `declare_parameter` | A typed input to an artefact, with an optional default. An output parameter also carries a value in and gives one back. | `READONLY` | 6 | ✓ |
| `declare_variable` | A typed variable, null until assigned unless given an initial value. | `DECLARE` | 64 | ✓ |
| `declare_row_set_variable` | A variable holding a row set. A rollback does not undo changes to it. |  | 8 |  |
| `create_temp_table` | A table private to the session, visible to called routines, dropped when the routine that created it returns. May declare keys and constraints. | `KEY` | 10 | ✓ |
| `create_global_temp_table` | A temp table visible to every session. Reported. |  | 0 |  |
| `drop_temp_table` | Drop a temp table before its routine ends. |  | 9 | ✓ |
| `declare_label` | A named point in a statement list that a jump can target. |  |  | ✓ |

### Assignment and control flow

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `assign` | Evaluate an expression and store it in a variable, converting to the variable's type. A value too long for a string variable is cut without an error. | `SET` | 44 | ✓ |
| `assign_from_rows` | Set variables from a query's rows: no row leaves them unchanged; several rows leave the values of the last row processed, which is undefined. |  | 15 | ✓ |
| `assign_from_single_value` | Set a variable from a query that must return at most one value: no row gives null; more than one row is an error. |  | 2 |  |
| `if` | Run one branch when the condition is true and the other when it is false or unknown. | `IF`, `ELSE` | 61 | ✓ |
| `while` | Repeat a block while the condition is true. False or unknown ends it. | `WHILE` | 14 | ✓ |
| `break` | Leave the innermost loop. | `BREAK` | 2 |  |
| `continue` | Go to the next pass of the innermost loop. | `CONTINUE` | 1 |  |
| `jump` | Go to a label. | `GOTO` | 1 | ✓ |
| `return` | End the artefact. A procedure gives back an integer code, zero if none is stated; a function gives back its value. | `RETURN` | 56 | ✓ |
| `call_procedure` | Run another procedure with arguments by name or position, receive its output parameters and optionally its return code. Its result sets go to the caller's caller. | `EXEC`, `EXECUTE` | 24 | ✓ |
| `emit_result_set` | Send a query's rows to the caller as the next result set, in the query's order if it has one. | `SELECT` |  | ✓ |
| `wait` | Pause for a duration or until a time of day. | `WAITFOR` | 0 |  |

### Errors and messages

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `try_catch` | Run a block; if a catchable error occurs in it, run the handler. Not catchable: messages of severity 10 or below, and an error resolving a name at the same level as the block. | `TRY`, `CATCH` | 28 | ✓ |
| `raise_error` | Raise an error with a number, message, severity and state. Severity 11 or above transfers to a handler. | `RAISERROR`, `THROW` | 12 |  |
| `rethrow` | Inside a handler, raise the error being handled again, unchanged. |  | 11 | ✓ |
| `emit_message` | Send text to the caller's message stream without raising an error. | `PRINT` | 20 | ✓ |
| `error_field` | Inside a handler, one field of the error being handled: number, message, severity, state, line or procedure. | `ERROR_NUMBER`, `ERROR_MESSAGE`, `ERROR_SEVERITY`, `ERROR_STATE`, `ERROR_LINE`, `ERROR_PROCEDURE` | 5 | ✓ |
| `last_statement_error` | The error number of the statement just run, zero if it succeeded. Every statement resets it. | `@@ERROR` | 1 | ✓ |
| `set_abort_on_error` | Choose whether an error ends the batch and rolls back the transaction (on) or ends only the statement (off). | `XACT_ABORT` | 32 | ✓ |

### Transactions

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `begin_transaction` | Start a transaction, or nest one level deeper in an open one. | `TRANSACTION` | 13 | ✓ |
| `commit_transaction` | Close one level. Only closing the outermost level makes the changes permanent. | `COMMIT` | 13 | ✓ |
| `rollback_transaction` | Undo every change since the outermost level began and close all levels. | `ROLLBACK` | 19 | ✓ |
| `save_point` | Name a point inside a transaction. | `SAVE` | 2 |  |
| `rollback_to_save_point` | Undo changes made since a named point, leaving the transaction open. |  |  |  |
| `transaction_depth` | How many levels are open. | `@@TRANCOUNT` | 13 |  |
| `transaction_state` | Whether a transaction is open and can still commit: 1, 0 or -1. | `XACT_STATE` | 7 |  |
| `set_isolation_level` | Choose what concurrent changes a transaction may see. Recorded; under the single-writer assumption it changes nothing. | `ISOLATION` | 1 |  |

### Cursors

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `declare_cursor` | Bind a cursor to a query, with its scope, whether it sees later changes (static, keyset, dynamic, or undefined for fast-forward) and whether it can update. |  | 10 | ✓ |
| `open_cursor` | Run the cursor's query and position before the first row. | `OPEN` | 10 | ✓ |
| `fetch_cursor` | Move to a row (next, prior, first, last, absolute, relative) and store its columns in variables. | `FETCH` | 10 | ✓ |
| `fetch_status` | Whether the last fetch found a row: 0, or -1 past the end, -2 if the row is gone. | `@@FETCH_STATUS` | 10 | ✓ |
| `close_cursor` | Release the cursor's rows. | `CLOSE` | 10 | ✓ |
| `deallocate_cursor` | Remove the cursor. | `DEALLOCATE` | 10 | ✓ |
| `write_current_row` | Update or delete the row the cursor is on. |  | 0 |  |

### Session state

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `rows_affected` | How many rows the statement just run read or changed. Every statement resets it. | `@@ROWCOUNT`, `ROWCOUNT` | 22 | ✓ |
| `last_identity(scope)` | The last identity value generated: in this routine's scope, anywhere in the session, or for a named table by any session. | `SCOPE_IDENTITY`, `@@IDENTITY`, `IDENT_CURRENT` | 3 | ✓ |
| `next_sequence_value` | Take the next number from a named sequence. Numbers are never reused, including after a rollback. |  | 2 |  |
| `set_session_option` | Change a setting that alters how later statements behave: language, first day of the week, null comparison, explicit identity values. | `NOCOUNT`, `LANGUAGE`, `DATEFIRST`, `IDENTITY_INSERT` | 74 | ✓ |
| `session_value` | A value belonging to the session or the machine: the login, the host, the database name, the session number, or a named value set earlier. | `ORIGINAL_LOGIN`, `SUSER_SNAME`, `HOST_NAME`, `DB_NAME`, `@@SPID`, `SESSION_CONTEXT`, `SERVERPROPERTY` | 8 |  |
| `role_membership` | Whether the current user belongs to a role. | `IS_ROLEMEMBER`, `IS_MEMBER` | 1 |  |
| `catalog_lookup` | A fact about a database object looked up by name or number at run time. | `OBJECT_ID`, `SCHEMA_NAME`, `OBJECT_NAME` | 4 |  |

### Raised forms

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `for_each_row` | Run a block once per row of a query, in the query's order, over a snapshot taken at the start. Raised from the fetch-loop idiom. |  |  | ✓ |
| `atomic_region` | A block whose changes all happen or none do. Raised from a begin and commit that enclose it on every path. |  |  | ✓ |
| `named_result` | A query's rows under a name, fixed at the point of definition. Raised from a temp table that is filled once and then only read. |  |  | ✓ |
| `column_expression_function` | A scalar function with no loop and no data access, restated as a single expression over its parameters. |  |  | ✓ |

## Query operations

The query part is a logical plan: each query is a sequence of these steps, keeping the source's names and order.

### Reading rows

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `read_table` | All rows of a table, view, temp table or row-set variable, in no particular order. | `FROM` |  | ✓ |
| `read_changed_rows` | In a trigger, the rows as they are after the statement (inserted) or as they were before it (deleted). |  | 12 | ✓ |
| `read_values` | A row set written out in the code. | `VALUES` | 12 |  |
| `call_table_function` | The rows a table-valued function returns for given arguments. |  |  |  |
| `read_as_of` | A table's rows as they were at a point in time, or over a period, by the table's own period columns. | `SYSTEM_TIME` | 9 |  |
| `search_text_index` | Rows of a table matching a full-text condition, each with a rank. | `CONTAINSTABLE`, `FREETEXTTABLE`, `CONTAINS`, `FREETEXT` | 1 |  |
| `shred_json` | Turn JSON text into rows. | `OPENJSON` | 1 |  |
| `shred_xml` | Turn the nodes of an XML value that match a path into rows. | `OPENXML` | 6 |  |
| `named_query` | A query given a name for use later in the same statement. | `WITH` | 8 | ✓ |
| `recursive_named_query` | A named query that refers to itself: an anchor, then a step repeated until it adds no rows or a stated depth is reached. |  | 4 |  |

### Relational steps

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `filter` | Keep the rows for which a condition is true. False and unknown both drop the row. | `WHERE`, `HAVING` |  | ✓ |
| `project` | Compute named columns from each row. |  |  | ✓ |
| `join(kind)` | Pair rows of two inputs that satisfy a condition: inner, left, right, full or cross. A null never equals a null. | `JOIN`, `ON` | 60 | ✓ |
| `lateral_join(kind)` | For each row of the left input, evaluate the right input using that row, and pair them. 'outer' keeps left rows with no match. | `APPLY` | 6 |  |
| `aggregate` | Group rows by key expressions and compute one row per group. With no keys, exactly one row even for empty input. Null keys form one group. | `GROUP` | 13 | ✓ |
| `aggregate_grouping_sets` | Aggregate at several levels of grouping at once, including subtotals and a grand total. | `ROLLUP`, `CUBE`, `GROUPING` | 1 |  |
| `window` | Compute a value for each row from the rows around it, by partition, order and frame, without merging rows. | `OVER`, `PARTITION` | 5 | ✓ |
| `distinct` | Remove duplicate rows. For strings, 'duplicate' follows the column's comparison rule, and which spelling survives is undefined. | `DISTINCT` | 4 |  |
| `sort` | Order rows by keys. Nulls come first ascending. Strings order by their comparison rule. Order among equal keys is undefined. | `ORDER` | 31 | ✓ |
| `limit` | Keep the first n rows or n percent, optionally with every row that ties with the last. Without a sort, which rows is undefined. | `TOP` | 8 |  |
| `skip_and_limit` | Skip a number of sorted rows, then keep a number. | `OFFSET` | 0 |  |
| `union(all)` | The rows of both inputs, with or without duplicates removed. | `UNION` | 13 |  |
| `intersect` | Rows present in both inputs, duplicates removed. Nulls match nulls here. | `INTERSECT` | 0 |  |
| `except` | Rows of the first input not present in the second, duplicates removed. Nulls match nulls here. | `EXCEPT` | 0 |  |
| `pivot` | Turn the distinct values of one column into columns, aggregating another. | `PIVOT` | 1 |  |
| `unpivot` | Turn a set of columns into rows of name and value, dropping nulls. | `UNPIVOT` | 0 |  |
| `to_json` | Format a query's rows as one JSON text. | `FOR JSON` | 5 |  |
| `to_xml` | Format a query's rows as one XML value. | `FOR XML` | 0 |  |

### Writing rows

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `insert_rows` | Add rows to a table from a query or a list of values. Columns not named take their default or generated value. Constraints and triggers apply. | `INSERT`, `INTO` | 35 | ✓ |
| `update_rows` | Change columns of the rows of a table that satisfy a condition, optionally taking values from joined rows. If a row matches several joined rows, which one is used is undefined. | `UPDATE` | 33 | ✓ |
| `delete_rows` | Remove the rows of a table that satisfy a condition, optionally chosen by joining other rows or through a named query. | `DELETE` | 9 | ✓ |
| `merge_rows` | Compare a table with a source on a condition and, per clause, update or delete matched rows, insert unmatched source rows, and update or delete target rows with no source. A target row matched by several source rows is an error for an update. | `MERGE`, `USING` | 2 |  |
| `return_changed_rows` | From a write, give back one row per row inserted, updated or deleted, with its values before and after and the action taken, to the caller or into a table. | `OUTPUT` | 3 |  |
| `create_table_from_query` | Create a table with a query's columns and fill it with its rows. |  |  |  |
| `truncate_table` | Remove every row of a table and reset its identity counter. No trigger fires. | `TRUNCATE` | 1 |  |
| `bulk_load` | Load rows from a file. | `BULK` | 0 |  |

## Expression operations

### Values and references

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `literal` | A typed constant. |  |  | ✓ |
| `null` | The absent value, with a type. |  |  | ✓ |
| `column` | A column of a named input, resolved against the catalog or scope. |  |  | ✓ |
| `variable` | The current value of a variable or parameter. |  |  | ✓ |
| `scalar_query` | The single value of a query used as an expression: null for no row, an error for more than one. |  |  |  |
| `call_scalar_function` | The value of a scalar function for given arguments. |  |  | ✓ |
| `updated_column_test` | In a trigger, whether the statement named a column in its list of columns to set, whatever value it set. | `UPDATE(` | 7 | ✓ |

### Conversion

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `convert` | Change a value to a type by SQL Server's rules. A value that cannot convert is an error. | `CAST`, `CONVERT` | 28 | ✓ |
| `convert_with_style` | Convert between text and a date, number or binary using a numbered format. |  | 6 |  |
| `try_convert` | As convert, giving null where the value cannot convert. | `TRY_CONVERT`, `TRY_CAST`, `TRY_PARSE` | 1 |  |
| `implicit_convert` | A conversion the source did not write, added by lowering wherever SQL Server's type precedence makes one happen. |  |  | ✓ |
| `fit_to_type` | Make a value fit a declared type on assignment or storage: cut a string without error, pad a fixed string, round a date and time, truncate money. |  |  | ✓ |

### Arithmetic

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `add` | Sum of two numbers, typed by precedence. Overflow is an error. |  |  | ✓ |
| `subtract` | Difference of two numbers. Overflow is an error. |  |  | ✓ |
| `multiply` | Product of two numbers. Overflow is an error. |  |  |  |
| `integer_divide` | Quotient of two integers, truncated toward zero. Division by zero is an error. |  |  | ✓ |
| `decimal_divide` | Quotient of exact decimals, with SQL Server's result precision and scale. Division by zero is an error. |  |  |  |
| `money_multiply` | Product involving money, truncated to four places. |  |  | ✓ |
| `money_divide` | Quotient involving money, truncated to four places. |  |  | ✓ |
| `float_divide` | Quotient of floating-point numbers. |  |  |  |
| `modulo` | Remainder, with the sign of the dividend. |  |  |  |
| `negate` | The number with its sign changed. |  |  |  |
| `round(digits, truncate)` | Round half away from zero to a number of digits, or truncate; the result keeps the input's type. | `ROUND` | 4 |  |
| `numeric_function(name)` | Absolute value, ceiling, floor, power, square root, sign, logarithms, each typed as SQL Server types it. | `ABS`, `CEILING`, `FLOOR`, `POWER`, `SQRT`, `SIGN`, `LOG`, `EXP` | 0 |  |
| `is_numeric_text` | Whether text would convert to any numeric type by SQL Server's rules, which accept currency signs, a lone plus or minus, and exponents. | `ISNUMERIC` | 0 |  |
| `random_number` | A pseudo-random number. Cannot be reproduced. | `RAND` | 0 |  |
| `new_guid` | A new unique identifier. Cannot be reproduced. | `NEWID`, `NEWSEQUENTIALID` | 0 |  |

### Comparison and logic

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `compare(operator, rule)` | Equal, not equal, less, greater and their combinations, giving true, false or unknown. Unknown whenever either side is null. For strings 'rule' is the collation's comparison rule. |  |  | ✓ |
| `compare_null_as_value` | Equality in which null equals null, as written under ANSI_NULLS off. |  |  |  |
| `is_null` | Whether a value is null. Never unknown. |  |  | ✓ |
| `and` | True if both are true, false if either is false, otherwise unknown. | `AND` |  | ✓ |
| `or` | True if either is true, false if both are false, otherwise unknown. | `OR` |  |  |
| `not` | True becomes false, false becomes true, unknown stays unknown. |  |  | ✓ |
| `between` | Whether a value lies between two bounds, both included. | `BETWEEN` | 7 | ✓ |
| `in_list` | Whether a value equals any of a list. Unknown if it equals none and the list contains a null. | `IN` |  | ✓ |
| `in_query` | Whether a value equals any value a query returns, with the same null behaviour. |  |  |  |
| `exists` | Whether a query returns at least one row. Never unknown. | `EXISTS` | 18 | ✓ |
| `like(rule)` | Whether text matches a pattern with T-SQL's wildcards: percent, underscore, bracketed sets and ranges, and an optional escape character. Follows the comparison rule. | `LIKE` | 6 |  |
| `case` | The result of the first branch whose condition is true, else the default, else null. Typed by the highest-precedence branch. | `CASE`, `IIF`, `CHOOSE` | 17 | ✓ |
| `first_non_null` | The first argument that is not null, typed by the highest-precedence argument. | `COALESCE` | 16 |  |
| `null_replacement` | The first argument unless it is null, then the second, converted to the first argument's type. | `ISNULL` | 16 | ✓ |
| `null_if_equal(rule)` | Null if the two arguments compare equal, else the first. | `NULLIF` | 3 | ✓ |

### Text

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `concat_null_propagating` | Join strings; null if any part is null. |  |  | ✓ |
| `concat_null_as_empty` | Join values as text, treating null as an empty string. | `CONCAT`, `CONCAT_WS` | 3 |  |
| `length_without_trailing_spaces` | Number of characters, not counting spaces at the end. | `LEN` | 4 | ✓ |
| `byte_length` | Number of bytes a value occupies as stored. | `DATALENGTH` | 2 |  |
| `upper` | Text in upper case. | `UPPER` | 4 | ✓ |
| `lower` | Text in lower case. | `LOWER` | 0 |  |
| `trim(side)` | Text with spaces removed from the left, the right or both. | `LTRIM`, `RTRIM`, `TRIM` | 8 | ✓ |
| `substring` | Part of a string from a position counted from 1 for a length. A start below 1 shortens the result by the shortfall. | `SUBSTRING` | 2 | ✓ |
| `left_part` | The first n characters. | `LEFT` | 2 |  |
| `right_part` | The last n characters. | `RIGHT` | 0 |  |
| `position_of(rule)` | Where one string first occurs in another from a starting position, counted from 1; 0 if absent. | `CHARINDEX` | 2 | ✓ |
| `pattern_position(rule)` | Where a pattern first matches in a string; 0 if nowhere. | `PATINDEX` | 0 |  |
| `replace_text(rule)` | Every occurrence of one string replaced by another, matched by the comparison rule. | `REPLACE`, `TRANSLATE` | 3 |  |
| `overlay` | Remove a length of characters at a position and insert a string there. Null when the start is below 1 or past the end, or the length is negative. | `STUFF` | 1 | ✓ |
| `repeat_text` | A string repeated n times. | `REPLICATE`, `SPACE` | 1 |  |
| `reverse_text` | A string with its characters in reverse order. | `REVERSE` | 0 |  |
| `character_from_code` | The character for a code, in the database's code page or in Unicode. |  |  |  |
| `code_of_character` | The code of the first character. | `ASCII`, `UNICODE` | 0 |  |
| `quote_identifier` | A name wrapped in delimiters with any delimiter inside it doubled. | `QUOTENAME` | 5 |  |
| `format_value` | A number or date as text by a format pattern and culture. | `FORMAT`, `STR` | 0 |  |
| `split_text` | Rows of the pieces of a string between separators. | `STRING_SPLIT` | 0 |  |

### Dates and times

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `current_time(kind)` | The moment the statement started: local time to 1/300 of a second, local time to 100 nanoseconds, or UTC. The same throughout one statement. | `GETDATE`, `SYSDATETIME`, `GETUTCDATE`, `SYSUTCDATETIME`, `CURRENT_TIMESTAMP` | 14 |  |
| `date_add(part)` | A date or time moved by a whole number of a unit. Moving by months clamps to the last day of the month. | `DATEADD` | 7 | ✓ |
| `date_diff_boundaries(part)` | How many boundaries of a unit lie between two moments. Not elapsed time: 23:59 to 00:01 is one day. | `DATEDIFF`, `DATEDIFF_BIG` | 4 | ✓ |
| `date_part(part)` | One field of a date or time as a number. Week and weekday depend on the session's first day of the week. | `DATEPART`, `YEAR`, `MONTH`, `DAY` | 2 |  |
| `date_name(part)` | One field of a date as text in the session's language. | `DATENAME` | 0 |  |
| `date_from_parts` | A date or time built from numbers. An impossible date is an error. | `DATEFROMPARTS`, `DATETIMEFROMPARTS`, `DATETIME2FROMPARTS` | 1 |  |
| `end_of_month` | The last day of a date's month. | `EOMONTH` | 0 |  |
| `is_date_text` | Whether text would convert to a date and time under the session's language and date format. | `ISDATE` | 0 |  |
| `change_offset` | A date and time moved to, or labelled with, an offset from UTC or a named time zone. | `SWITCHOFFSET`, `TODATETIMEOFFSET` | 0 |  |

### Aggregates and window functions

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `count_rows` | Number of rows, as a 32-bit integer; overflow is an error. | `COUNT` | 11 | ✓ |
| `count_values` | Number of non-null values of an expression, optionally distinct by its comparison rule. |  |  | ✓ |
| `sum` | Total of non-null values, typed as SQL Server types it: an integer total stays a 32-bit integer and overflows as an error. Null for no values. | `SUM` | 19 | ✓ |
| `average` | Total of non-null values divided by their count, typed as SQL Server types it: over integers the result is an integer, truncated. Null for no values. | `AVG` | 3 | ✓ |
| `minimum` | Smallest non-null value; for strings by the comparison rule. | `MIN` | 10 |  |
| `maximum` | Largest non-null value; for strings by the comparison rule. | `MAX` | 3 |  |
| `join_text` | Non-null values joined with a separator, in a stated order. | `STRING_AGG` | 0 |  |
| `statistical_aggregate(name)` | Standard deviation or variance, sample or population. | `STDEV`, `STDEVP`, `VAR`, `VARP` | 0 |  |
| `row_number` | 1, 2, 3 within a partition in a stated order. Order among ties is undefined. | `ROW_NUMBER` | 2 | ✓ |
| `rank(dense)` | Position within a partition, ties sharing a position, with or without gaps after them. | `RANK`, `DENSE_RANK` | 1 |  |
| `bucket_number` | Which of n nearly equal groups a row falls in. | `NTILE` | 0 |  |
| `offset_value(direction)` | A value from a row a number of positions before or after the current one. | `LAG`, `LEAD` | 0 |  |
| `frame_value(end)` | The first or last value in the window frame. | `FIRST_VALUE`, `LAST_VALUE` | 0 |  |

### Other values

| Operation | Meaning | Comes from | Artefacts | Hand |
|---|---|---|---|---|
| `hash_bytes(algorithm, encoding)` | A hash of a value's bytes. 'encoding' states how text became bytes, which differs between single-byte and UTF-16 strings. | `HASHBYTES`, `CHECKSUM` | 2 |  |
| `compress_bytes(direction)` | GZip compression or decompression. | `COMPRESS`, `DECOMPRESS` | 1 |  |
| `json_value(kind)` | A scalar or a fragment taken from JSON text by a path, a modified copy of the text, or whether text is valid JSON. | `JSON_VALUE`, `JSON_QUERY`, `JSON_MODIFY`, `ISJSON` | 2 |  |
| `xml_value(kind)` | A scalar or a fragment taken from an XML value by a path expression, or whether a path matches. |  | 8 |  |

## What in the corpus is deliberately not converted

These have a place in the representation and no conversion. Each is recorded and reported as manual work.

| Node kind or type | Artefacts | Why |
|---|---|---|
| `dynamic_sql_execution` | 15 | A recognised construct: the text is built at run time, so there is nothing to lower. |
| `schema_change` | 10 | Schema migration is out of scope. |
| `permission_change` | 0 | Security is outside what the tool converts. |
| `external_reference` |  | The object is outside the catalog. |
| `unsupported_type(source_name)` | 2 | No neutral meaning yet for these source types. |

## Operations the corpus uses that the hand conversion did not exercise

These are on paper only, and the corpus needs them. The most used come first.

| Operation | Artefacts |
|---|---|
| `first_non_null` | 16 |
| `current_time(kind)` | 14 |
| `union(all)` | 13 |
| `transaction_depth` | 13 |
| `read_values` | 12 |
| `raise_error` | 12 |
| `minimum` | 10 |
| `read_as_of` | 9 |
| `xml_value(kind)` | 8 |
| `session_value` | 8 |
| `limit` | 8 |
| `declare_row_set_variable` | 8 |
| `transaction_state` | 7 |
| `shred_xml` | 6 |
| `like(rule)` | 6 |
| `lateral_join(kind)` | 6 |
| `convert_with_style` | 6 |
| `to_json` | 5 |
| `quote_identifier` | 5 |
| `round(digits, truncate)` | 4 |
| `recursive_named_query` | 4 |
| `distinct` | 4 |
| `catalog_lookup` | 4 |
| `return_changed_rows` | 3 |
| `replace_text(rule)` | 3 |
| `maximum` | 3 |
| `concat_null_as_empty` | 3 |
| `save_point` | 2 |
| `next_sequence_value` | 2 |
| `merge_rows` | 2 |
| `left_part` | 2 |
| `json_value(kind)` | 2 |
| `hash_bytes(algorithm, encoding)` | 2 |
| `date_part(part)` | 2 |
| `byte_length` | 2 |
| `break` | 2 |
| `assign_from_single_value` | 2 |
| `try_convert` | 1 |
| `truncate_table` | 1 |
| `shred_json` | 1 |
| `set_isolation_level` | 1 |
| `search_text_index` | 1 |
| `role_membership` | 1 |
| `repeat_text` | 1 |
| `rank(dense)` | 1 |
| `pivot` | 1 |
| `date_from_parts` | 1 |
| `continue` | 1 |
| `compress_bytes(direction)` | 1 |
| `aggregate_grouping_sets` | 1 |

## What the check found

Every built-in function called, every `@@` value and every type that appears in the corpus is accounted for by an entry above.

What that does and does not show:

- It shows no built-in function, `@@` value or type in the corpus lacks an entry.
- It does not show the entries are right. Nothing lowers into this vocabulary yet, and no target generates from it.
- It cannot see operations chosen by meaning. Whether a `/` is integer or decimal division depends on types, which pattern matching does not have.
- Words that are syntax and not operations, and five table aliases from XML shredding that look like function calls, are listed in the JSON so the check knows they were considered.
