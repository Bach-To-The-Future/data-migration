---
status: accepted
---

# The test-data generator never sees converted code

The testing module exists to catch coincidental correctness: wrong logic that gives the right answer on the test data. We decided that the data generator, and any LLM that proposes test inputs, sees only the catalog and the original artefact, never the converted code or anything derived from it. That holds even for mutation testing: when a planted fault survives, the response is more data from the same blind generator, not data built to expose that fault. We chose this because test data shaped by the conversion can end up fitted to it, and an unbiased test is worth more than a higher mutation score.

The full decision is in [the resolution of the ticket that made it](https://github.com/Bach-To-The-Future/data-migration/issues/20#issuecomment-6142928591).

## Considered options

- **Targeting data at each surviving mutant.** It kills more mutants, and is what mutation-testing tools usually do. Rejected: a mutant is a variant of the converted code, so data designed against it is data designed from the conversion.

## Consequences

- **Some mutants survive that targeted data would have killed.** If survivors remain after a fixed budget of extra blind data, the verdict is weakened to "Reconciled, weakly tested", which does not count as converted.
- **Coverage is measured on the original.** Extra inputs are aimed at branches and statements of the original that were not reached. Coverage of the converted code is reported but never used to choose data.
- **What is checked also comes from the original.** The list of statements and expressions to compare is taken from the original by the parser; the conversion's links only say where each counterpart is.
- **The same rule binds later additions.** Anything that generates test inputs, including inputs added when the tool learns a lesson, must be built from the catalog and the original.

## Amendment, 14 October 2026

Two sources of test inputs now come from converted code:

- **Candidate disagreements.** Raising and lowering assist produce several candidates. Those that pass every check are run against each other, and any input on which they disagree becomes a new test input.
- **Mutants.** A surviving mutant may now prompt an input built to expose it.

The rule above is relaxed for them, and only this far: **the blind generator alone owns the hold-out seeds that decide a verdict.** Inputs derived from converted code may be added to the visible set only. They can make tests stricter, by finding a failure, and can never decide a pass.

This replaces "never data built from the mutant itself" with "never in the hold-out set". The reason in the decision above still holds where it matters: a pass is only ever decided by data the conversion could not have shaped. The change was made in [Which autonomy level does each component get?](https://github.com/Bach-To-The-Future/data-migration/issues/14).
