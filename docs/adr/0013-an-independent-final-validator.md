---
status: accepted
---

# An independent final validator decides, with the testing module, what counts as converted

The testing module is built to be independent of conversion, but it is still part of the same tool. Its data generator, comparison code and tolerances are versioned with the conversion they judge, and repair and lessons work against its evidence. A defect in it would let a wrong conversion through, with nothing to catch it. We decided that a final validator runs after the consistency pass as an independent audit. An artefact counts as converted only if its verdict is "Reconciled" **and** it passes final validation.

- **Inputs.** The validator is given only:
  - the original source from the catalog snapshot;
  - the delivered project exactly as shipped;
  - everything needed to recreate the original's database faithfully. The validator builds that database with its own code from the snapshot's raw definitions (captured DDL and settings text), never from the catalog's parsed facts, so an extraction mistake cannot fool both judges.

  It never sees the intermediate representation, source links, the run plan, the testing module's verdicts, seeds or data, lessons, worked examples, repair history or intent summaries.
- **A fresh sandbox.** It runs the delivered project there, so that packaging and environment faults are caught too.
- **No shared code with the testing module.** It has its own data generator, with a different algorithm and its own randomness, and its own comparison code. Only the shipped support library is shared, because the delivered code imports it; the library is part of what is judged, not part of the judge. In the build, the validator is written by an agent that has not seen the testing module's code.
- **Deterministic.** SQL Server execution of the original is the only judge, and no LLM decides anything.
- **One-way, apart from a pass-or-fail bit.** Only its pass or fail result may be used, and only by gates: lesson confirmation and worked-example qualification. Its inputs, evidence and failure details never reach any conversion component. The pilot is validated before the bulk starts, so that the pilot's lessons and examples are validated before they are used.
- **A failure.** It marks the artefact "Failed final validation", which does not count as converted, and flags a defect in the testing module. The failing input is treated like a disclosed hold-out seed: the maintainers may see it, it is never added to the testing module's data, and it is retired. A fix to the testing module counts only if the testing module then catches the failure with its own blind data. Repair acts on that artefact only after such a fix.
- **Its own storage.** Validator seeds are single-use. They and all its evidence live in its own schema of the engagement's control database, under its own role, which the engine's role cannot read. Validator replay uses only that role.

We chose this because a second judge that shares nothing with the first is the only way to catch the first judge's own mistakes. We accept the cost of an extra full execution pass for it.

The mechanics are decided in [How does the final independent validator work?](https://github.com/Bach-To-The-Future/data-migration/issues/32).

## Considered options

- **Trust the testing module alone**, protected by its independence rules, hold-out seeds and mutation testing. Cheaper by one full execution pass. Rejected: every one of those safeguards is designed and versioned together with the module it protects, so a mistake in that design is shared by all of them.
- **A second testing-module run with different seeds.** It is cheap to build. Rejected: it shares the generator's algorithm, the comparison code and the tolerances, which is where a defect would hide.

## Consequences

- **"Converted" now means "Reconciled" and passed final validation.** This amends [ADR 0012](0012-only-reconciled-counts-as-converted.md). In a preview run nothing is converted anyway.
- **Every source and target must provide a second, independent runner** for the validator, beside the testing module's. For the Oracle and Snowflake paper designs, that is a second Oracle-side and a second Snowflake-side execution path.
- **A validator failure stops that artefact until a tool release** that improves the testing module, with one exception. If the artefact was delivered in a raised form, it falls back deterministically to its faithful form, which is validated again with fresh seeds.
- **The benchmark's quality metric is the share of artefacts converted**, meaning "Reconciled" and passed final validation.
- **Wall-clock time rises** by one full execution pass per run. The orchestrator benchmark measures speed including it, the same for both orchestrators.
- **The validator is a separate deliverable in the build**, with its own author, its own code and its own tests.

Amended on 16 October 2026 with the owner's answers on the ticket: inputs from raw definitions, its own schema and role, disclosure treated like a hold-out seed, the faithful fallback, the pass-or-fail bit for gates, and the benchmark metric.
