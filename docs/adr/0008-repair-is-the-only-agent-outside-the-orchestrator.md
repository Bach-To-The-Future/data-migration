---
status: accepted
---

# Outside the orchestrator, AI works as LLM steps, and repair is the only agent

The owner's hypothesis was a multi-agent system: modules using AI agents between deterministic stages, coordinated by an orchestrator agent. We decided that, for every component except the orchestrator, AI takes part in five places (lowering assist, the hard cases of raising, the generation fallback, proposing extra test inputs, and narrating the report), each as an LLM step that ordinary code calls, checks and retries. Exactly one component, repair, is an agent. We chose the least autonomous level that meets the correctness bar for each component because the evidence pointed that way: in the hand conversion 68 of 69 faithful steps were rule-shaped, and the only real agent loops found in existing tools are per-artefact convert, check and retry loops, which is what repair is.

The full decision, with the level, the evidence and what would move each component, is in [the resolution of the ticket that made it](https://github.com/Bach-To-The-Future/data-migration/issues/14).

## Considered options

- **Agents in every module that uses AI.** Rejected: a model choosing its own steps is harder to make repeatable and cacheable, and nothing in lowering assist, raising or the fallback needs to explore. Each gets its context assembled for it and returns output a deterministic check verifies.
- **No agent at all, repair included.** Rejected for now: repair has to decide where to look, what to try and whether to try again from evidence. It stays an agent unless the build benchmark shows a single fixed-prompt call, given the same pinpointed evidence, fixes as many failures.

## Consequences

- **The orchestrator is still open.** Whether it is an agent is settled by the build benchmark, not here.
- **A retried call is not an agent.** An LLM step stays one when code calls it several times for candidates or again after a failed check; this is now how the glossary defines it.
- **Rules come first, and their failures are not handed to a model.** If a raising rule's output fails a check, the faithful form is delivered and the rule is reported as faulty.
- **Every AI output is checked by deterministic code before it counts**, and falls back when it runs out of attempts: to the faithful form for raising, to the flagged fallback or manual work for lowering assist, to manual work for repair.
- **Levels can move.** A hard raising case that an LLM step gets right every time across the corpus becomes a rule; repair moves down if the benchmark says so.
