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
- Any change to any extracted fact, including its provenance, gives a new snapshot identifier. Cache keys are unaffected: they hash the facts a conversion read, never the identifier (spec section 3).
- The hash covers the server's name, so the same databases on a rebuilt container with a new host name are a new snapshot.
