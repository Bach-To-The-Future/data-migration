---
status: accepted
---

# A live server is the only source of its databases' objects

The spec lets an extraction take one live server and any number of folders, and says query batches and SSIS packages are files even when a server is there. It does not say what happens when a folder beside the server also holds data definition (tables, views, routines) or job scripts for what the server holds. We decided that the server is then the only source of those objects. For a database read from the server, a folder's data-definition statements are not replayed, and a job script is not read, because msdb already holds the jobs. Every such statement and file is kept in the raw capture and listed as superseded in the report data, with the reason. Folders beside a server add only what a server never holds: query batches and SSIS packages. A folder for a database that the server extraction does not read is refused, because one catalog covers one engagement and adding a database means a new extraction.

Made while building [extraction from folders](https://github.com/Bach-To-The-Future/data-migration/issues/40).

## Considered options

- **Merge the two, with the server winning on conflict.** Objects only in the files would join the catalog. But an object that the server lacks is either dropped or never deployed, which is exactly the "confirmation that an object exists" a server gives. Merging would put objects in the catalog that the client's database does not have, mix provenance within one database, and need a conflict rule for every fact.
- **Merge, with the files winning.** This trusts an export over the database it was exported from. Exports go stale, and the spec makes the live server the primary path.
- **The server alone, chosen.** One source per database, so every fact about a database's objects has the same provenance and every object is known to exist. The cost is that a folder's newer, undeployed definitions are ignored. They are listed, so the operator can see this and extract again once the definitions are deployed.

## Consequences

- With a server, a folder's data definition for its databases changes nothing in the catalog. The report data's `extraction.folders.superseded` lists each statement.
- A database is read either from the server or from folders alone, never both. Its default collation, compatibility level and settings come from that one source.
- SQL Agent jobs come from msdb when there is a server, and from job scripts only without one. Their references to catalog objects are worked out the same way from either.
- Without a server, the SQL Server release and each database's compatibility level are settings too. When they are not given, SQL Server 2022 (the sandbox's release) and its default level, 160, are assumed and recorded as assumed, like every other fact a server would have supplied.
