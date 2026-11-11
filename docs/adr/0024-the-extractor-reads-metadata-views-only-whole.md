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
- The raw capture holds columns that change with nobody acting on the client's objects, such as `sys.databases.log_reuse_wait`, job run history in `msdb` and partition row counts. The snapshot's identifier leaves them out, and with them a table's row count, which only the report reads ([ADR 0023](0023-a-snapshots-identifier-is-the-hash-of-its-content.md), amended). So do an identity's `last_value` and a sequence's `current_value` and `last_used_value`, which move with the client's rows. Each read hashes the one fact it read, not the whole object.

## Decided with it by the owner

- **Row-derived counters are metadata, like row counts.** `sys.identity_columns.last_value` and `sys.sequences.current_value` are kept in the raw capture with every other column. They are never copied into a fact, and the snapshot's identifier leaves them out (ADR 0023, amended). The spec allows row counts from `sys.partitions` beside "nothing derived from table rows", and these counters are read the same way.
- **`INSERT` reported on a temporal history table is not excess.** SQL Server's permission functions report `INSERT` on every history table to a login that is granted nothing there (seen on WideWorldImporters, SQL Server 2022 CU27). The `INSERT` itself is refused with error 13559 while system versioning is on, which is the only time a table is a history table. The self-check therefore does not count it, but records each one in the database's `permission_exemptions` fact and in the report's extraction section. Any other permission, on a history table or elsewhere, still fails the self-check. Without this, the self-check refused the whole public-corpus database.

## Amendment, 11 November 2026: fact-building code may be shared with the folder extraction

"Builds every fact from that capture" says where facts come from, not where the code that shapes them lives. The server extraction and the extraction from folders ([ADR 0025](0025-a-live-server-is-the-only-source-of-its-databases-objects.md)) fill the same catalog, and each had built the same facts with the same shapes (columns, constraints, parameters, triggers, types) in its own module. The owner decided that they may share the code that turns a neutral description (a column, a key, a parameter) into a fact, provided:

- **The server adapter feeds the builders only from raw-capture rows.** It still reads each view only as `SELECT * FROM <view>`, from the one fixed list, and every description it hands to a builder is made from those captured rows and nothing else. Moving the step from description to fact changes neither what is read nor what the raw capture holds. A fact's provenance still names the views it came from.
- **The folders adapter feeds them from the replayed definitions,** and keeps the assumed values to itself: the builders assume nothing.
- **The final validator never uses these builders** ([ADR 0013](0013-an-independent-final-validator.md)). It rebuilds the original's database from the snapshot's raw definitions with its own code, never from the catalog's parsed facts, so a mistake in a builder cannot reach both judges. A structure test keeps every module but the two adapters from importing them.

Made while planning the refactor in [issue #108](https://github.com/Bach-To-The-Future/data-migration/issues/108).
