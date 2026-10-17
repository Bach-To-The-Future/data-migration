---
status: accepted
---

# The product's tools are free, and open source wherever possible

The owner set a rule on 17 October 2026: every tool the product uses must be free, and open source wherever possible. The Claude models are excepted.

- **Containers** run on Podman, or on Docker Engine in WSL2, never on Docker Desktop.
- **SQL Server connections** use Microsoft's ODBC driver. It is free but not open source, a recorded exception. For versions it can't connect to (at least 2000 and 2005, with the boundary confirmed in the build's VM tests), FreeTDS is the fallback driver.
- **Already compliant:** Python, PySpark, Delta Lake, Postgres, pgvector, sqlglot, ScriptDom (MIT) and .NET, and Z3.

The other recorded exceptions are the source and target platforms themselves:

- **SQL Server,** with the Developer edition as the oracle;
- **Databricks:** the client's target, plus the optional remote tier and the vendor's release-test workspace, both of them optional;
- **Windows VMs,** with licensed installation media, for the build's tests of SQL Server 2000 to 2016.

We chose this because a product sold to many clients shouldn't carry licence costs or terms that each client, or each engagement, has to clear. Wherever a free and open tool exists, the product uses it.

## Considered options

- **Docker Desktop.** It is the most familiar choice on Windows. Rejected: it is not free for commercial use at the vendor's size.
- **FreeTDS for every SQL Server version.** It is fully open source. Rejected as the main driver: Microsoft's ODBC driver is what SQL Server's own tools and documentation assume for current versions, so it stays, as a recorded exception, with FreeTDS only for versions it can't reach.

## Consequences

- **The sandbox and the corpus build must run on Podman.** They were built on Docker, and running them on Podman is still to be established.
- **The product's runners use the ODBC driver.** Behaviour observed with `pymssql` in the corpus tools, such as result sets lost after a failed statement, has to be established again for ODBC.
- **Any new tool must meet the rule,** or be added to the recorded exceptions with its reason.
