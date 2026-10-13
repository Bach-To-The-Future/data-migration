---
status: accepted
---

# The core works on its own typed intermediate representation

Every source must work with every target without pairwise converters. We decided that the core defines and owns a typed intermediate representation in which each operation has one explicit meaning whatever source it came from: sources lower into it, targets generate from it. We chose it over letting an LLM translate source text to target text because a source's silent behaviours (integer division, case-insensitive comparison, `money` arithmetic) must be detected by code from catalog types, not recalled by a model, and because the per-construct conversion report needs constructs identified structurally.

The terms used here (intermediate representation, lowering, raising, generation, opaque fragment, scope barrier) are defined in `CONTEXT.md`. The full decision, with the component list, is in [the resolution of the ticket that made it](https://github.com/Bach-To-The-Future/data-migration/issues/11#issuecomment-6141555476).

## Considered options

- **A parser's syntax tree as the shared form.** Rejected: no one parser reads every source, and the trees that work are structurally unlike each other, so every target would have to understand every source's tree.
- **No shared representation: an LLM translates source text to target text.** Rejected: knowledge of the source's quirks reaches the target only through a prompt, so each source/target pairing is only as good as its prompt. That is pairwise conversion hidden in prompts.
- **A procedural skeleton, with each statement left as source SQL text for an LLM.** Rejected for the same reason: the statements are where the silent differences live.
- **Adopting sqlglot's tree for the query part.** Rejected: it is shaped like SQL syntax, has no operations with explicit meaning, a library upgrade could change the contract, and in testing it dropped part of a `MERGE` without reporting it. sqlglot and ScriptDom are used as tools inside a source or a target, never as the contract between them.

## Consequences

- **This is the most work of the options.** The vocabulary of operations and types is ours to design and maintain.
- **Depth is a dial, not a cliff.** Whatever lowering cannot express is kept verbatim as an opaque fragment and handled by an LLM step, so the representation can start shallow and deepen. Nothing is ever dropped silently.
- **The core changes by addition only.** A new source or target may add operations and may never change what an existing one means. A target either handles an operation or declares it unsupported, which the report shows as manual work.
- **Lowering never interprets intent.** A cursor is lowered as a cursor. Rewriting it into a set-based step is raising: a separate, optional transform in the core that is recorded in the report and can be switched off without touching a source or a target.
- **Queries are relational steps, not SQL clauses**, keeping the source's names and order. PySpark's DataFrame API is itself a chain of relational steps, so generation for the first target is close to one-to-one.
- **Lowering depends on the catalog** for types, collations and scopes, so nothing can be converted without an extraction first.
- **One place still holds pairwise knowledge:** the fallback in which an LLM step returns target code for an opaque fragment. Its output is always flagged in the report.
