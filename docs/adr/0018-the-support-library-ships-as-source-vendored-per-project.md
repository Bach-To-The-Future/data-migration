---
status: accepted
---

# The support library ships as source, vendored in each project under versioned names

Converted code imports the support library at runtime, so a client can't run its migrated code without it. We decided:

- **Source-available.** The library's source ships inside every delivered project, under a source-available licence: perpetual, for the client's internal use and for modification in their own maintenance, with no redistribution and no use to build a competing tool.
- **One pure-Python wheel.** It has no third-party dependencies of its own. The project carries the wheel with its hash and pins it exactly. There is no dependency on a package index.
- **Versioned names.** Each project vendors the library under names that include its full version: the import name (for example `tsqlrt_v1_4_3`), the distribution name, Spark function names and temp-table names. Projects pinned to different versions can therefore share one Databricks environment.
- **One shared state table.** The counter state table, for identity values and row versions, is shared by every version. Its schema is versioned and only ever extended.

We chose this because a client will not migrate onto code it cannot keep running, read or maintain if the vendor goes away. Exact pinning per project keeps every project running on the exact code it was verified with.

The full decision is in the resolution of [How is the support library packaged, versioned and tested?](https://github.com/Bach-To-The-Future/data-migration/issues/22).

## Considered options

- **A closed binary library.** It protects the product best. Rejected: clients couldn't audit or maintain the code their business runs on.
- **A fully open licence.** It is simplest for clients. Rejected by the owner: it would let the library be used to build a competing tool.
- **One import name with a shared installation.** The usual Python practice. Rejected: two projects verified against different versions couldn't share an environment without one running on code it wasn't verified with.

## Consequences

- **Several versions can sit side by side in one environment.** Each is used only by the projects pinned to it.
- **A fix reaches deployed code only by redeployment.** Each release ships a defect advisory list, and a `check-project` command shows which artefacts use an affected helper.
- **Two projects writing the same tables share counters** through the one state table. The single-writer assumption still applies.
