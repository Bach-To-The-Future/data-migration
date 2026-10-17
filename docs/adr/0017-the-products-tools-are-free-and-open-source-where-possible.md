---
status: accepted
---

# The product's tools are free, and open source wherever possible

The owner set a rule on 17 October 2026: every tool the product uses must be free, and open source wherever possible. The Claude models are excepted.

- **Containers.** The product runs on Docker Engine (or Podman) and never depends on Docker Desktop. Docker Desktop may still be used on the owner's development machine, where its licence permits.
- **SQL Server connections** use Microsoft.Data.SqlClient (MIT), called from Python through pythonnet, for the runners and the extractor. The final validator uses Microsoft's JDBC driver, mssql-jdbc (MIT), so the two judges share no client. For versions these can't connect to (at least 2000, with the boundary confirmed in the build's VM tests), FreeTDS is the fallback, and artefacts that continue past an error on such a server are reported as having an incomplete observation.
- **Already compliant:** Python, PySpark, Delta Lake, Postgres, pgvector, sqlglot, ScriptDom (MIT) and .NET, and Z3.

The other recorded exceptions are the source and target platforms themselves:

- **SQL Server,** with the Developer edition as the oracle;
- **Databricks:** the client's target, plus the optional remote tier and the vendor's release-test workspace, both of them optional;
- **Windows VMs,** with licensed installation media, for the build's tests of SQL Server 2000 to 2016.

We chose this because a product sold to many clients shouldn't carry licence costs or terms that each client, or each engagement, has to clear. Wherever a free and open tool exists, the product uses it.

## Considered options

- **Requiring Docker Desktop.** It is the most familiar choice on Windows. Rejected as a product dependency: it is not free for commercial use at the vendor's size. It stays allowed as a convenience on a development machine, because nothing the product ships may rely on it.
- **Microsoft's ODBC driver through `pyodbc`.** It was chosen first. Rejected after testing: it is not open source, and through `pyodbc` it loses every result set, row count and return value after a failed statement that the server carries on from, as `pymssql` does. SqlClient, with errors delivered as messages, and mssql-jdbc, reading on after each error, return them all ([Observe the ODBC driver after a failed statement](https://github.com/Bach-To-The-Future/data-migration/issues/34)).
- **FreeTDS for every SQL Server version.** It is fully open source. Rejected as the main driver: it is kept only for versions the Microsoft clients can't reach.

## Consequences

- **The sandbox and the corpus build must be proved on plain Docker Engine.** They were built on Docker Desktop. The proof is a build-phase check recorded in the spec, for example a Linux CI job that rebuilds the sandbox and the corpus and must write an identical `corpus/manifest.json`.
- **The ODBC driver stays in the toolbox image only as the test record.** No product component uses it, and the ODBC exception is removed.
- **Each judge cross-checks its client** against Extended Events' per-statement row counts from the server. A mismatch is a client defect: it is reported, and the artefact does not count as converted.
- **Any new tool must meet the rule,** or be added to the recorded exceptions with its reason.

Amended on 17 October 2026: the rule binds the product, not the owner's development machine, where Docker Desktop may still be used.

Amended again on 17 October 2026: Microsoft.Data.SqlClient replaces the ODBC driver, and the final validator uses mssql-jdbc.
