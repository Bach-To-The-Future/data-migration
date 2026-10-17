# Annex F. The extractor's grant, revoke and check scripts

Part of the [v1 architecture spec](v1-architecture-spec.md), section 3. Copied unchanged from [the extractor-permissions research](../research/extractor-permissions.md), which holds the evidence: the grant set was tested on SQL Server 2017 CU31, 2019 CU32, 2022 CU27 and 2025 CU9. For SQL Server 2000 the login gets database access only; its exact script is confirmed in the build's VM tests.

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

[mv]: https://learn.microsoft.com/en-us/sql/relational-databases/security/metadata-visibility-configuration
[desc]: https://learn.microsoft.com/en-us/sql/relational-databases/system-dynamic-management-views/sys-dm-exec-describe-first-result-set-for-object-transact-sql
[deps]: https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-sql-expression-dependencies-transact-sql
[tz]: https://learn.microsoft.com/en-us/sql/t-sql/functions/current-timezone-transact-sql
[parts]: https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-partitions-transact-sql
[agent]: https://learn.microsoft.com/en-us/sql/ssms/agent/sql-server-agent-fixed-database-roles
