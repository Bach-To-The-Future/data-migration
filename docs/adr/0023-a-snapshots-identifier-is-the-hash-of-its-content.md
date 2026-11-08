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

## Amended 8 November 2026: columns the server changes by itself

The raw capture ([ADR 0024](0024-the-extractor-reads-metadata-views-only-whole.md)) holds columns SQL Server changes with nobody acting. On CI's freshly started server, one of them moved between two preview runs, which gave a new snapshot identifier and so report data that was not byte-identical outside its run-specific section. Decided by the owner on [issue #39](https://github.com/Bach-To-The-Future/data-migration/issues/39): the identifier is derived from each object's content less the cells listed below.

- Every cell is still kept, and each object's full content hash is still checked when it is read back.
- A re-extraction that differs only in these cells is the same snapshot, so the stored values are kept. Its `extraction` row lists the objects that differed (`differing_objects`), so nothing is lost silently.
- Nothing may read these cells. The core's query interface refuses the fact that holds them, and a test checks that no fact built from the raw capture changes when they do. The final validator, which shares no code with the core (ADR 0013), checks the same in its own tests.
- Counters that move with the client's data are not listed: an identity's `last_value`, a sequence's `current_value` and partition row counts move only when something changed.
- The list changes only by a recorded decision, here. `engine/tests/test_full_extraction.py` holds the extractor's list to this table.

| View | Columns the identifier leaves out |
|---|---|
| `sys.databases` | `log_reuse_wait`, `log_reuse_wait_desc` |
| `dbo.sysjobschedules` | `next_run_date`, `next_run_time` |
| `dbo.sysjobsteps` | `last_run_outcome`, `last_run_duration`, `last_run_retries`, `last_run_date`, `last_run_time` |
