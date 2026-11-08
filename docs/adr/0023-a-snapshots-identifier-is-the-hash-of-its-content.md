---
status: accepted
---

# A snapshot's identifier is the hash of its content

The spec gives each extraction an immutable snapshot with its own identifier, gives each object a content hash, and requires that re-extracting an unchanged server invalidate nothing. We decided that a snapshot's identifier is derived from its objects' identities and content hashes (`snap-` and 20 hex digits of a SHA-256), so extracting an unchanged server again yields the same snapshot, which is stored once. Every extraction is still recorded, in its own append-only `extraction` table, pointing at the snapshot it produced. Reading a snapshot back recomputes every content hash and the identifier and fails loudly on any mismatch, which catches rows added to a stored snapshot as well as changed ones; changes and deletions are refused outright by triggers.

Made while building [the engine skeleton](https://github.com/Bach-To-The-Future/data-migration/issues/36).

## Considered options

- **A fresh identifier per extraction** (a timestamp or random id). It keeps the spec's wording literally, but two extractions of the same server then name two snapshots with identical content, and a warm re-run's report data names a different snapshot each time, so it can never be byte-identical outside its run-specific section (Acceptance Criteria, selling point 4) unless the snapshot is moved there.
- **Content-derived, chosen:** one identifier per distinct content, so a report naming the snapshot stays identical across re-extractions of an unchanged server, and the identifier itself is a check on what is stored.

## Consequences

- "Each extraction" in the spec is recorded as an extraction row, not as a new snapshot. A run still uses exactly one snapshot, and the report names it.
- Provenance is part of every object's content hash, so a live extraction and a folder extraction of the same facts, whose provenance differs (read from the server, parsed from a file, assumed from a setting), never share an identifier. Accepted by the owner on that condition; `engine/tests/test_catalog.py` pins it.
- Any change to any extracted fact, including its provenance, gives a new snapshot identifier. Cache keys are unaffected: they hash the facts a conversion read, never the identifier (spec section 3).
- The hash covers the server's name, so the same databases on a rebuilt container with a new host name are a new snapshot.

## Amended 8 November 2026: what the server changes by itself, and row counts

The raw capture ([ADR 0024](0024-the-extractor-reads-metadata-views-only-whole.md)) holds columns that change with nobody acting on the client's objects. On CI's freshly started server, some moved between two preview runs: Query Store, on by default in SQL Server 2022, records the extractor's own queries in internal tables, whose row counts are in `sys.partitions`. That gave a new snapshot identifier, and so report data that was not byte-identical outside its run-specific section. Decided by the owner on [issue #39](https://github.com/Bach-To-The-Future/data-migration/issues/39): the identifier is derived from each object's content less the cells and facts listed below.

- Everything is still kept, and each object's full content hash is still checked when it is read back.
- A re-extraction that differs only there is the same snapshot, so the stored values are kept. Its `extraction` row lists the objects that differed (`differing_objects`), so nothing is lost silently.
- A table's row count is a volatile informational fact. It is kept in the snapshot, left out of the identity, and read only by the report, for example for its warning on row-by-row code over a large table. Conversion, the testing module and the final validator never read it, so it never affects a cache key. The raw `sys.partitions.rows` it is built from is left out with it.
- Nothing but the report may read anything listed here. The core's query interface refuses it, and only report code may call `catalog.report_value`, which a structure test checks. A test rebuilds the catalog with all of it changed and requires every other fact to be identical. The final validator, which shares no code with the core (ADR 0013), checks the same in its own tests.
- The counters that move with the client's rows are listed too: an identity's `last_value`, and a sequence's `current_value` with `last_used_value`, which moves on the same draw. They are kept in the raw capture and never copied into a fact ([ADR 0024](0024-the-extractor-reads-metadata-views-only-whole.md)), so inserting rows or drawing a sequence value gives the same snapshot. Decided by the owner on issue #39, replacing the first form of this amendment, which kept them in the identity.
- These lists change only by a recorded decision, here. `engine/tests/test_full_extraction.py` holds the extractor's lists to these tables.

| View | Raw-capture columns the identifier leaves out |
|---|---|
| `sys.databases` | `log_reuse_wait`, `log_reuse_wait_desc` |
| `sys.identity_columns` | `last_value` |
| `sys.sequences` | `current_value`, `last_used_value` |
| `dbo.sysjobschedules` | `next_run_date`, `next_run_time` |
| `dbo.sysjobsteps` | `last_run_outcome`, `last_run_duration`, `last_run_retries`, `last_run_date`, `last_run_time` |
| `sys.partitions` | `rows` |

| Object kind | Informational facts the identifier leaves out, read only by the report |
|---|---|
| `table` | `row_count` |
