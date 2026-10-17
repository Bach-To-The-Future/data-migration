---
status: accepted
---

# Mappings shared by a source and a target are split into a source half and a target half

The destination requires every source to work with every target through one core, with no pairwise converters. Walking Oracle PL/SQL and Snowflake SQL through the design on paper showed three pieces of machinery that would still have grown pairwise. Each was written for SQL Server and PySpark together:

- **Error identity.** The support library mapped Spark's exceptions straight to SQL Server error numbers, so a failed integer division meant "error 8134". For Oracle the same failure is `ORA-01476`.
- **The silent-differences catalogue,** which compares T-SQL with Spark.
- **The fixed type mapping,** from SQL Server types to Spark types.

We decided that each is split into a half per source and a half per target, which meet at the core's vocabulary:

- **Errors.** The core defines neutral **error classes**: divide by zero, key violation, no data found, conversion failure and so on. Each target maps its runtime's failures to classes. Each source maps classes to its own codes, messages and severities. `raise_error` keeps its SQL Server meaning, and Oracle adds `raise_exception(code, message)`.
- **The differences catalogue.**
  - The source half says how source constructs behave, in vocabulary terms. Lowering and the generator's edge-case pools use it.
  - The target half says where the target's built-ins differ from vocabulary meanings. The support library, tolerances and mutation use it.
  - The existing entries are re-filed unchanged. An entry such as floating-point aggregation order belongs to both halves.
- **The type mapping.** Source types map to vocabulary types per source. Vocabulary types map to target types, with representation rules, per target.

We chose this because S sources and T targets then need S + T of each, not S × T. SQL Server's behaviour on Databricks does not change.

The full decision is in the resolution of [What must a new source or target provide?](https://github.com/Bach-To-The-Future/data-migration/issues/17). The paper design is in [`docs/spec-drafts/source-target-contract.md`](../spec-drafts/source-target-contract.md).

## Considered options

- **Keep the pairwise versions, and write one per pair when a second source or target arrives.** Simplest for v1. Rejected: it breaks "no pairwise converters" at exactly the point the destination exists to prove, and each new pair would repeat work already done for its source and its target.

## Consequences

- **The core gains error classes.** The vocabulary's error operations refer to them, and a new source or target must map them.
- **The catalogue and the mappings are restructured** when the spec is assembled. No entry's content changes.
- **The support library holds the target half only.** Its version still identifies the mappings it ships ([ADR 0018](0018-the-support-library-ships-as-source-vendored-per-project.md)), and its name says which target it serves.
- **Meanings that are explicitly SQL Server's stay.** Examples are decimal scale rules and counting string length in UTF-16 units. A source whose rules differ adds its own types and operations.
