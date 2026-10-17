---
status: accepted
---

# Databases below compatibility level 100 are reconciled only on a guarded test server

Legacy clients still run databases at compatibility levels 80 and 90, the levels of SQL Server 2000 and 2005, on old servers and on 2008 to 2012 servers that keep them. Every SQL Server with a container image (2017, 2019, 2022 and 2025) refuses those levels. All four were tested on 17 October 2026, and each accepts levels from 100 upwards only. So the sandbox cannot be a faithful oracle for such a database.

We decided that each database is routed by its compatibility level:

- **Level 100 or above on 2017 or later:** a container of the client's own version.
- **Level 100 or above on 2008 to 2016:** a 2022 container at the client's level, with the report listing the known behaviour differences that compatibility levels don't cover.
- **Level 80 or 90:** these artefacts are converted. They are reconciled only on an optional **source-side remote tier**, a dedicated test server of that version that receives synthetic data only. Otherwise they are "Not reconcilable locally".

**The test server is guarded**, and the tool refuses to run unless every check passes:

- **The login:** a tester login, never the extractor's, holding no `sysadmin` or other server-level role. It is granted the right to create databases directly, and keeps the right to see every database.
- **The server's contents:** the server holds no databases other than system databases and the tool's own scratch databases.
- **The server's identity:** it differs from the server the snapshot was extracted from.

We chose this because reconciliation writes data and runs code. The only place it may do that is a server shown, by these checks, to hold nothing of the client's.

The full decision is in [the resolution of the ticket that made it](https://github.com/Bach-To-The-Future/data-migration/issues/26#issuecomment-6233615401).

## Considered options

- **Reconcile old-level databases on the client's own server.** It would be faithful by construction. Rejected: it would run converted and original code, and write synthetic rows, on a server that may be production. That breaks the promise that the tool never executes client code there.
- **Raise old databases to level 100 in the sandbox.** It works for most code. Rejected: level 100 changes behaviour, and `*=` joins stop parsing at all (tested), so the oracle would no longer be the client's behaviour.
- **Refuse versions before 2008.** Simpler. Rejected: those are the systems most in need of migration.

## Consequences

- **"Remote tier" now covers sources as well as targets.** ADR 0014's remote tier was a target account; this one is a source server.
- **Pre-2005 syntax is converted but can only be verified on the test server**, for example `*=` joins.
- **Old test servers are unsupported software.** The build tests them on Windows VMs on an isolated network, and licensed installation media for each version is a prerequisite.
