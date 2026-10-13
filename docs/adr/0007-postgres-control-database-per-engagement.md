---
status: accepted
---

# Everything the tool persists lives in a Postgres control database, one per engagement

The tool has to keep the catalog, the run catalog, observations, verdicts and a client's lessons between runs. We decided that a Postgres control database is the source of truth for all of it, with one database per engagement and catalog snapshots stored as immutable rows with content hashes. A file export of a snapshot exists so two snapshots can be compared with ordinary diff tools, and it is derived, never the source of truth. This gives everything an engagement produces one transactional, queryable home, a hard boundary between clients, and a place for vector similarity later.

The full decision is in [the resolution of the ticket that made it](https://github.com/Bach-To-The-Future/data-migration/issues/8#issuecomment-6144234513).

## Considered options

- **Plain text files as the source of truth, with a database index rebuilt from them.** This was the first recommendation: files are readable by a person, comparable with ordinary tools, and need no server. The owner chose the database as the source of truth and kept the readable, comparable form as an export.
- **One shared database with a client key on every row.** Rejected: separation would then depend on every query remembering the key.

## Consequences

- **Every run needs Postgres**, including one with reconciliation off. Locally that means Docker. This is a real change from a command-line tool that needed only Python, accepted because delivery engineers run the tool.
- **Deleting a client's data is dropping a database.** No query can cross clients by mistake.
- **Global lessons do not live there.** They ship with the tool, separately.
- **Snapshots never change.** A run uses exactly one. Name adjustments are recorded once outside any snapshot, so later extractions keep them.
- **Vector similarity is for finding similar artefacts only.** It never decides which artefacts a lesson affects; that stays an exact structural match.
