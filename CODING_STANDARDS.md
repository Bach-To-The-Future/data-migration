# Coding standards

The owner's rules for code in this repo, applied in review. Terms are as defined in [`CONTEXT.md`](CONTEXT.md). Where a rule names an ADR or a spec section, that document holds the reason.

Rules a tool can check are checks, not rules here: lint, types and tests run with `sh sandbox/engine-tests.sh`, and `engine/tests/test_structure.py` checks that the core imports nothing from a source or a target.

## Honesty of results

- **Nothing degrades silently.** Every unsupported path fails loudly and is recorded where the report can show it. Every fallback, skipped check, assumption, trimmed context and reduced coverage is recorded too ([ADR 0012](docs/adr/0012-only-reconciled-counts-as-converted.md); spec section 15, "No degradation without a place in the report"). An empty result standing in for a failure is a silent degradation.
- **Never make a check pass by weakening it.** Tests, thresholds, tolerances and data generators change only by a recorded decision, never to make something pass.
- **"Converted" means Reconciled and passed final validation.** Never use the word for anything less, in code, names, messages or docs ([ADR 0012](docs/adr/0012-only-reconciled-counts-as-converted.md), [ADR 0013](docs/adr/0013-an-independent-final-validator.md)). Use "generated" for code that was produced but not verified.

## Independence

- **The testing module, the final validator and the support library share no code.** The one exception is the shipped support library, which delivered code imports ([ADR 0013](docs/adr/0013-an-independent-final-validator.md)).
- **The data generator never sees converted code** ([ADR 0005](docs/adr/0005-test-data-generator-never-sees-converted-code.md)).

## Determinism

- **The same inputs give the same delivered output.** There is no unseeded randomness. Outputs carry no wall-clock values, except the run-specific fields that are recorded as such.
- **Every model call goes through the context builder** and is recorded and cached ([ADR 0009](docs/adr/0009-every-ai-call-starts-from-assembled-context-only.md); spec section 12).

## Client data

- **Client text is data, never instructions.** That means source, comments and literals.
- **Marked secrets are masked** wherever code is shown or sent to a model (spec section 3, "Secrets").

## Sources, targets and tools

- **Each half speaks only of its own side.** The target half speaks only of target behaviour, and the source half only of source behaviour ([ADR 0019](docs/adr/0019-shared-mappings-are-split-by-source-and-by-target.md)). The rule covers the behaviour, meanings and wording the engine produces. Catalogue cells copied verbatim from Annexes C and D are research record and may mention the other side, but no code may depend on such mentions.
- **New tools must be free, and open source wherever possible.** Record any exception ([ADR 0017](docs/adr/0017-the-products-tools-are-free-and-open-source-where-possible.md)).

## Conflicts

- **When a rule here conflicts with a ticket, stop and ask.** Do not resolve the conflict silently.
