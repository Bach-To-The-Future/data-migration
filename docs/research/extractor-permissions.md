# SQL Server permissions for the extractor

Research for [What SQL Server permissions does the extractor need?](https://github.com/Bach-To-The-Future/data-migration/issues/25), 13 October 2026.

## Answer

The extractor can read everything the catalog holds, except one item, with a grant set that gives no ability to read table rows, write, or run code.

**The minimum grant set**

1. A login, with a user in each database being extracted.
2. `VIEW DEFINITION` on each of those databases.
3. `SELECT` on the system view `sys.sql_expression_dependencies` in each of those databases. This is only for the dependency cross-check and can be left out.
4. A user in `msdb`, with `SELECT` on five SQL Server Agent tables: `sysjobs`, `sysjobsteps`, `sysschedules`, `sysjobschedules`, `syscategories`.

Nothing at server level. No fixed role of any kind.

**The one item it cannot cover: a procedure's result shape.** SQL Server will describe a procedure's first result set only to a login that holds `EXECUTE` on that procedure, and a login that holds `EXECUTE` can run it. That contradicts the promise that the extractor never executes client code, since the promise would then rest on the tool's behaviour and not on what the login is able to do. See [Result shapes](#result-shapes-need-execute) for the alternatives.

**How this was established.** The grant set was tested on SQL Server 2022 (16.0.4295.3, CU27, Developer edition, Linux container). A test login was given the grants in stages, and at each stage every read was compared with what `sa` reads. Then seventeen things the login must not be able to do were attempted, and all were refused or returned nothing. Behaviour on SQL Server 2016 to 2019 was not tested and rests on documentation where stated.

## What each catalog item needs

"Verified" means the test login's result equalled `sa`'s on the sandbox with the grants shown. All tests ran on SQL Server 2022.

| Catalog item | Source | Needs | Evidence |
|---|---|---|---|
| Server version, edition, collation | `SERVERPROPERTY` | Nothing beyond a login | Verified |
| Nested-triggers setting | `sys.configurations` | Nothing beyond a login | Verified; the view is visible to `public` ([metadata visibility][mv]) |
| Server time zone | `CURRENT_TIMEZONE()`, `CURRENT_TIMEZONE_ID()` | Nothing beyond a login | Verified on 2022. Documented for SQL Server 2022 only ([CURRENT_TIMEZONE][tz]) |
| Database collation, compatibility level, `RECURSIVE_TRIGGERS`, default `ANSI` settings | `sys.databases` | A user in the database | Verified |
| Tables, columns, declared types, nullability, collation | `sys.tables`, `sys.columns`, `sys.types` | `VIEW DEFINITION` | Verified |
| Column defaults, check constraints, computed columns, with their expressions | `sys.default_constraints`, `sys.check_constraints`, `sys.computed_columns` | `VIEW DEFINITION` | Verified. The expression is `NULL` without it ([metadata visibility][mv]) |
| Identity seed and increment | `sys.identity_columns` | `VIEW DEFINITION` | Verified |
| Primary key and unique constraints, indexes | `sys.key_constraints`, `sys.indexes`, `sys.index_columns` | `VIEW DEFINITION` | Verified |
| Foreign keys with their actions | `sys.foreign_keys`, `sys.foreign_key_columns` | `VIEW DEFINITION` | Verified |
| Source text of procedures, views, functions, triggers | `sys.sql_modules.definition` | `VIEW DEFINITION` | Verified |
| Stored `ANSI_NULLS` and `QUOTED_IDENTIFIER` | `sys.sql_modules` | `VIEW DEFINITION` | Verified |
| Parameters with type and direction | `sys.parameters` | `VIEW DEFINITION` | Verified |
| Columns of views and table-valued functions | `sys.columns` | `VIEW DEFINITION` | Verified |
| Triggers, their actions and kind, firing order | `sys.triggers`, `sys.trigger_events` | `VIEW DEFINITION` | Verified |
| User-defined types, table types, sequences | `sys.types`, `sys.table_types`, `sys.sequences` | `VIEW DEFINITION` | Verified |
| Synonyms | `sys.synonyms` | `VIEW DEFINITION`, expected | Not verified: the sandbox has no synonyms |
| Row counts | `sys.partitions.rows` | `VIEW DEFINITION` | Verified. The count is approximate ([sys.partitions][parts]) |
| SQL Server's dependency metadata | `sys.sql_expression_dependencies` | `VIEW DEFINITION` and `SELECT` on that view | Verified. Refused with error 229 without the `SELECT` grant, as documented ([dependencies][deps]) |
| SQL Agent jobs, steps, schedules | `msdb.dbo.sysjobs`, `sysjobsteps`, `sysschedules`, `sysjobschedules`, `syscategories` | `SELECT` on each | Verified |
| First result set of a procedure | `sys.dm_exec_describe_first_result_set_for_object` | `EXECUTE` on the procedure | Verified: see below |

Two details worth knowing:

- **Missing permission is silent.** With a user but no `VIEW DEFINITION`, every object-level query returned zero rows, not an error. Microsoft documents this: "Queries on system views might only return a subset of rows, or sometimes an empty result set" ([metadata visibility][mv]). The extractor should therefore check its own permissions before reading, and not treat an empty result as an empty database.
- **`sys.dm_db_partition_stats` is not needed.** It was refused with error 297 until `VIEW DATABASE STATE` was granted. `sys.partitions` gives row counts without it.

## Result shapes need EXECUTE

Tested on the sample procedure `sales.uspGetOrdersByCustomer`, which `sa` describes as five columns:

| Grants held by the test login | Result |
|---|---|
| `VIEW DEFINITION` | No columns. The function returns error 229: "The EXECUTE permission was denied on the object" |
| `VIEW DEFINITION` and `VIEW DATABASE STATE` | The same |
| `VIEW DEFINITION` and `SELECT` on the tables it reads | The same |
| `VIEW DEFINITION` and `EXECUTE` on the procedure | Five columns, as `sa` sees |

With `EXECUTE` granted, the login then ran the procedure and got rows back. Microsoft's page states the requirement as "Requires permission to execute the @tsql argument" ([describe][desc]).

Views and functions are not affected: their columns come from `sys.columns` under `VIEW DEFINITION`.

Alternatives, none of them tested:

1. **Describe shapes in the sandbox.** Reconciliation already creates the original procedures on a sandbox SQL Server, where the tool has full rights. Shapes can be described there, so the client's server is never asked. It needs the sandbox even for a run with reconciliation off.
2. **The client's database administrator exports them.** A script the administrator runs under their own account writes the shapes to a file, which the extractor reads as a folder input. The extractor's login stays minimal.
3. **Grant `EXECUTE` knowingly.** The extractor still would not run anything, but the login could. This gives up the property that the promise is enforced by the server.
4. **Go without.** The catalog already records "could not be described" with a reason, and reconciliation covers the shape when enabled.

## What the login could not do

All attempted as the test login holding the minimum grant set.

| Attempt | Outcome |
|---|---|
| Read rows from a table | Refused, error 229 |
| Read rows through a view | Refused, error 229 |
| Count a table's rows by scanning | Refused, error 229 |
| Call a table-valued function | Refused, error 229 |
| Read a statistics histogram (`sys.dm_db_stats_histogram`) | Returned no rows |
| `DBCC SHOW_STATISTICS` | Refused, error 2557 |
| Insert | Refused, error 229 |
| Update | Refused, error 229 |
| Delete | Refused, error 229 |
| Create a table | Refused, error 262 |
| Alter a procedure | Refused, error 3701 |
| Execute a procedure | Refused, error 229 |
| Create an Agent job | Refused, error 229 |
| Start an Agent job | Refused, error 229 |
| Update an Agent job row | Refused, error 229 |
| Create a database | Refused, error 262 |
| Read other logins' password hashes | Returned none |

The login's effective permissions, as reported by `fn_my_permissions`:

- **On a database:** `CONNECT`, `VIEW DEFINITION`, and four that come with them on SQL Server 2022: `VIEW SECURITY DEFINITION`, `VIEW PERFORMANCE DEFINITION`, `VIEW ANY COLUMN ENCRYPTION KEY DEFINITION`, `VIEW ANY COLUMN MASTER KEY DEFINITION`.
- **On a table:** `VIEW DEFINITION` only.
- **On the server:** `CONNECT SQL` and `VIEW ANY DATABASE`, which every login has.

`VIEW DEFINITION` on a database does let the login see the database's users, roles and permission assignments, as well as all code. A client should know that the extractor's login can read that, although the extractor has no use for it.

## What cannot be read at all

- **Encrypted routines.** A procedure created `WITH ENCRYPTION` showed a `NULL` definition to `sa` and to the test login alike, while `OBJECTPROPERTY(..., 'IsEncrypted')` returned 1 for both. The extractor can therefore record it as present but not extractable. Verified.
- **Linked-server connection strings.** Microsoft documents that `sys.servers.provider_string` is `NULL` without `ALTER ANY LINKED SERVER` ([metadata visibility][mv]). Not tested.

## Things that were considered and rejected

**The `msdb` role `SQLAgentReaderRole`.** It is the obvious way to read jobs, and it is not read-only. As a member, the test login read the jobs and then created one. Microsoft's table for the role lists "Create/modify/delete" on local jobs as "Yes (owned jobs only)" and says the role "includes all the SQLAgentUserRole permissions" ([Agent roles][agent]). A login that owns a job also cannot be dropped until the job is removed, which the test ran into. `SELECT` on the five tables reads the same information and nothing else. SQL Server Agent's tables are outside metadata visibility ([metadata visibility][mv]), which is why an explicit grant is needed.

**A server-wide grant in place of per-database users.** `CONNECT ANY DATABASE` with `VIEW ANY DEFINITION`, and no database users, read all metadata in all three sandbox databases, and reading rows, executing and inserting were still refused. It is simpler to grant, with these drawbacks:

- It covers every database on the server, not only those chosen for the engagement.
- It showed the login the names of other logins. Password hashes stayed hidden.
- On SQL Server 2022 it brought `VIEW ANY SECURITY DEFINITION` and `VIEW ANY PERFORMANCE DEFINITION` with it.
- The dependency view and the Agent tables were still refused, because their grants need a user in the database.

## Version differences

Only SQL Server 2022 was tested. From documentation:

- **`CURRENT_TIMEZONE()` is documented for SQL Server 2022 only** among on-premises versions ([CURRENT_TIMEZONE][tz]). On earlier versions the difference between `GETDATE()` and `GETUTCDATE()` gives the current offset, which is not the same as the time zone: it says nothing about daylight-saving rules. For those servers the zone is best supplied by the client as a setting, with the offset as a cross-check. Other ways to read the zone on older versions were not researched.
- **`VIEW SECURITY DEFINITION` and `VIEW PERFORMANCE DEFINITION`** exist from SQL Server 2022 ([metadata visibility][mv]). On earlier versions `VIEW DEFINITION` is the only permission of this kind.
- **Microsoft's current documentation starts at SQL Server 2017.** The pages consulted list no version earlier than that, so nothing here is established for SQL Server 2016.

## Not established

- Any behaviour on SQL Server 2016, 2017 or 2019, by test.
- Synonyms, because the sandbox has none.
- SSIS packages stored in a server's `SSISDB` or `msdb`. The catalog takes packages as files, so this was not examined.
- Whether `VIEW DEFINITION` granted on individual schemas, in place of the whole database, would be enough. It would narrow what the login can see.
- Windows-authenticated logins, contained databases and Azure SQL.
- The four alternatives for result shapes.

## Scripts

Replace the login name, and supply a password by the client's own means. None is given here.

**Grant**

```sql
-- Once, on the server
CREATE LOGIN [migration_extractor] WITH PASSWORD = '<set by the client>';

-- In each database being extracted
USE [<database>];
CREATE USER [migration_extractor] FOR LOGIN [migration_extractor];
GRANT VIEW DEFINITION TO [migration_extractor];
GRANT SELECT ON sys.sql_expression_dependencies TO [migration_extractor];  -- optional: dependency cross-check

-- For SQL Agent jobs
USE msdb;
CREATE USER [migration_extractor] FOR LOGIN [migration_extractor];
GRANT SELECT ON dbo.sysjobs         TO [migration_extractor];
GRANT SELECT ON dbo.sysjobsteps     TO [migration_extractor];
GRANT SELECT ON dbo.sysschedules    TO [migration_extractor];
GRANT SELECT ON dbo.sysjobschedules TO [migration_extractor];
GRANT SELECT ON dbo.syscategories   TO [migration_extractor];
```

**Revoke**

```sql
USE [<database>];   -- each database, and msdb
DROP USER [migration_extractor];

USE master;
DROP LOGIN [migration_extractor];
```

Dropping the user removes its grants with it.

**Checks a client can run, connected as the extractor's login**

```sql
USE [<database>];

-- 1. What the login holds. Expect CONNECT and the VIEW ... DEFINITION permissions, and nothing else.
SELECT permission_name FROM fn_my_permissions(NULL, 'DATABASE') ORDER BY permission_name;

-- 2. On any table. Expect VIEW DEFINITION only: no SELECT, INSERT, UPDATE or DELETE.
SELECT DISTINCT permission_name FROM fn_my_permissions('<schema>.<table>', 'OBJECT');

-- 3. Each of these must fail with error 229.
SELECT TOP 1 * FROM <schema>.<table>;
EXEC <schema>.<procedure>;

-- 4. On the server. Expect CONNECT SQL and VIEW ANY DATABASE only.
SELECT permission_name FROM fn_my_permissions(NULL, 'SERVER') ORDER BY permission_name;
```

These are the checks the test ran; the expected output above is what they returned on SQL Server 2022.

[mv]: https://learn.microsoft.com/en-us/sql/relational-databases/security/metadata-visibility-configuration
[desc]: https://learn.microsoft.com/en-us/sql/relational-databases/system-dynamic-management-views/sys-dm-exec-describe-first-result-set-for-object-transact-sql
[deps]: https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-sql-expression-dependencies-transact-sql
[tz]: https://learn.microsoft.com/en-us/sql/t-sql/functions/current-timezone-transact-sql
[parts]: https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-partitions-transact-sql
[agent]: https://learn.microsoft.com/en-us/sql/ssms/agent/sql-server-agent-fixed-database-roles
