---
status: accepted
---

# An independent final validator decides, with the testing module, what counts as converted

The testing module is built to be independent of conversion, but it is still part of the same tool. Its data generator, comparison code and tolerances are versioned with the conversion they judge, and repair and lessons work against its evidence. A defect in it would let a wrong conversion through, with nothing to catch it. We decided that a final validator runs after the consistency pass as an independent audit. An artefact counts as converted only if its verdict is "Reconciled" **and** it passes final validation.

- **Inputs.** The validator is given only:
  - the original source from the catalog snapshot;
  - the delivered project exactly as shipped;
  - everything needed to recreate the original's database faithfully. The validator builds that database with its own code from the snapshot's raw definitions (captured DDL and settings text), never from the catalog's parsed facts, so an extraction mistake cannot fool both judges. The raw definitions are folder DDL files kept verbatim, and the rows the extractor reads from each metadata view, captured wholesale.

  It never sees the intermediate representation, source links, the run plan, the testing module's verdicts, seeds or data, lessons, worked examples, repair history or intent summaries.
- **A fresh sandbox.** It runs the delivered project there, so that packaging and environment faults are caught too.
- **No shared code with the testing module.** It has its own data generator, with a different algorithm and its own randomness, and its own comparison code. Only the shipped support library is shared, because the delivered code imports it; the library is part of what is judged, not part of the judge. In the build, the validator is written by an agent that has not seen the testing module's code.
- **Deterministic.** SQL Server execution of the original is the only judge, and no LLM decides anything.
- **One-way, apart from a pass-or-fail bit.** Only its pass or fail result may be used, and only by gates: lesson confirmation and worked-example qualification. Its inputs, evidence and failure details never reach any conversion component. The pilot is validated before the bulk starts, so that the pilot's lessons and examples are validated before they are used.
- **A failure.** It marks the artefact "Failed final validation", which does not count as converted, and flags a defect in the testing module. The failing input is treated like a disclosed hold-out seed: the maintainers may see it, it is never added to the testing module's data, and it is retired. A fix to the testing module counts only if the testing module then catches the failure with its own blind data. Repair acts on that artefact only after such a fix.
- **Its own storage.** Validator seeds are single-use. They and all its evidence live in its own schema of the engagement's control database, under its own role, which the engine's role cannot read. Validator replay uses only that role.

We chose this because a second judge that shares nothing with the first is the only way to catch the first judge's own mistakes. We accept the cost of an extra full execution pass for it.

The mechanics are decided in [the resolution of How does the final independent validator work?](https://github.com/Bach-To-The-Future/data-migration/issues/32#issuecomment-6231461796).

## Considered options

- **Trust the testing module alone**, protected by its independence rules, hold-out seeds and mutation testing. Cheaper by one full execution pass. Rejected: every one of those safeguards is designed and versioned together with the module it protects, so a mistake in that design is shared by all of them.
- **A second testing-module run with different seeds.** It is cheap to build. Rejected: it shares the generator's algorithm, the comparison code and the tolerances, which is where a defect would hide.

## Consequences

- **"Converted" now means "Reconciled" and passed final validation.** This amends [ADR 0012](0012-only-reconciled-counts-as-converted.md). In a preview run nothing is converted anyway.
- **Every source and target must provide a second, independent runner** for the validator, beside the testing module's. For the Oracle and Snowflake paper designs, that is a second Oracle-side and a second Snowflake-side execution path.
- **A validator failure stops that artefact until a tool release** that improves the testing module, with one exception. If the artefact was delivered in a raised form, it falls back deterministically to its faithful form, which is validated again with fresh seeds.
- **The faithful fallback is followed by one short final step**, which re-verifies and re-validates the artefact and its dependents, at most once per artefact. If the faithful form also fails, the artefact is not converted and its dependents are blocked.
- **One capability, "validate a subset as a delivered project"** (the subset with its callees and triggers, packaged, in a fresh sandbox), is used three times in a run: after the pilot, on demand to confirm a lesson drafted during the bulk, and for the whole project at the end. Each use draws fresh seeds and costs a sandbox run.
- **The benchmark's quality metric is the share of artefacts converted**, meaning "Reconciled" and passed final validation.
- **Wall-clock time rises** by one full execution pass per run. The orchestrator benchmark measures speed including it, the same for both orchestrators.
- **The validator is a separate deliverable in the build**, with its own author, its own code and its own tests.

Amended on 16 October 2026 with the owner's answers on the ticket: inputs from raw definitions, its own schema and role, disclosure treated like a hold-out seed, the faithful fallback and its final step, the pass-or-fail bit for gates, the subset-validation capability, and the benchmark metric.

## Amendment, 17 October 2026: the validator's SQL Server client

The validator talks to SQL Server through Microsoft's JDBC driver, mssql-jdbc (MIT), reading on after each error. The testing module uses Microsoft.Data.SqlClient. Both were tested to return every result set, row count, message and return value after a failed statement that the server carries on from ([Observe the ODBC driver after a failed statement](https://github.com/Bach-To-The-Future/data-migration/issues/34)). So the two judges share no client. Each also cross-checks its client against Extended Events' per-statement row counts from the server. A mismatch is a client defect, reported, and the artefact does not count as converted.

For versions mssql-jdbc can't reach, the validator falls back to jTDS (LGPL), never FreeTDS, which the testing module uses. jTDS is unmaintained and is used only against the isolated old-version test servers, never a client's production server or the extracted server. It is tested in the build's VMs. Where it loses results after an error, the validation result is "Not validated: incomplete observation".

## Amendment, 18 October 2026: who may use the pass-or-fail bit

The validator's pass or fail result, and nothing else of its, may be used by five consumers: lesson confirmation, worked-example qualification, the pilot barrier, dependency blocking, and the orchestrator's observation. Its inputs, evidence and failure details still never reach any conversion component. Decided in [Independent review of the assembled spec](https://github.com/Bach-To-The-Future/data-migration/issues/28), finding R12.
