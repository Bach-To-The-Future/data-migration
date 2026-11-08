---
status: accepted
---

# The extractor reads metadata views only whole, and builds every fact from that capture

The spec has the extractor keep a raw capture of every metadata view it reads, "each view captured wholesale (every column)", "never additional views", and the final validator rebuilds the original's database from that capture with its own code. We decided that the extractor reads each view only as `SELECT * FROM <view>`, from one fixed list in `engine/src/migrt/sources/sqlserver/extractor.py`, and builds every catalog fact from those rows in Python. It runs no query of its own over the views: no joins, no filters other than one database's own row of `sys.databases`, and no functions over metadata such as `TYPE_NAME` or `OBJECTPROPERTY`. Apart from the views, it sends only the version check (`SERVERPROPERTY`), `CURRENT_TIMEZONE()` and the permission self-check. The connection records every batch it sends, and a sandbox test checks that list.

Made while building [full live extraction](https://github.com/Bach-To-The-Future/data-migration/issues/39).

## Considered options

- **Targeted queries for the facts, plus a separate wholesale capture.** Each fact is read by the query that suits it, as the first cut did. The raw capture is then a second read that can disagree with the facts, and nothing shows that the extractor read only listed views.
- **Wholesale reads only, chosen.** The raw capture holds exactly the rows every fact came from. "Every column, no other views" holds by construction and is checked against `sys.all_columns`. The cost is more data read (about 10 MB for the three corpus databases, in under ten seconds), and joins written in Python.

## Consequences

- A fact's provenance names the views it was built from, never a query.
- A new fact means either reading a listed view differently or adding a view to the list. Adding one is a change to what the extractor reads, and Annex F's grant set must still cover it.
- The raw capture holds columns SQL Server changes by itself, such as `sys.databases.log_reuse_wait` and job run history in `msdb`. The snapshot's identifier leaves them out, and nothing may read them ([ADR 0023](0023-a-snapshots-identifier-is-the-hash-of-its-content.md), amended). Counters that move with the client's data, such as an identity's `last_value` and a sequence's `current_value`, give a new snapshot identifier when they change, but invalidate no conversion. Each read now hashes the one fact it read, not the whole object, so a table's row count moving with its data does not invalidate a view that read only its columns.

## Decided with it by the owner

- **Row-derived counters are metadata, like row counts.** `sys.identity_columns.last_value` and `sys.sequences.current_value` are kept in the raw capture with every other column. They are never copied into a fact that conversion reads. The spec allows row counts from `sys.partitions` beside "nothing derived from table rows", and these counters are read the same way.
- **`INSERT` reported on a temporal history table is not excess.** SQL Server's permission functions report `INSERT` on every history table to a login that is granted nothing there (seen on WideWorldImporters, SQL Server 2022 CU27). The `INSERT` itself is refused with error 13559 while system versioning is on, which is the only time a table is a history table. The self-check therefore does not count it, but records each one in the database's `permission_exemptions` fact and in the report's extraction section. Any other permission, on a history table or elsewhere, still fails the self-check. Without this, the self-check refused the whole public-corpus database.
