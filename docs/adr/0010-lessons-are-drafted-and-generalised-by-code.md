---
status: accepted
---

# Lessons are drafted and generalised by code, not by a model

When repair fixes a failure, the tool can learn from it, and something must turn that one fix into a lesson that applies elsewhere. A model is the obvious choice: it could read the fix and write a general pattern. We decided that code does both steps.

- **Drafting.** A lesson is drafted from the repaired region:
  - its trigger is the region's pattern, with every identifier and literal replaced by a placeholder, matched exactly;
  - its symptom is the failing check's evidence;
  - its fix is the repaired region as a worked example, with repair's one-line hypothesis.
- **Generalising.** When two or more drafted lessons share a fix kind and a failure class, and their triggers differ only at some nodes, code computes their least general generalisation (anti-unification), with typed wildcards at the nodes that differ. The result is a new candidate lesson that must pass the confirmation gate on every artefact it matches.

We chose this because a pattern computed from real triggers matches only what they have in common, and because a model's widening is judgement the gate can only partly check.

The full decision is in [the resolution of the ticket that made it](https://github.com/Bach-To-The-Future/data-migration/issues/23#issuecomment-6211827394).

## Considered options

- **An LLM step that generalises a fix into a pattern.** It could write wider and more natural lessons. Rejected: it would be a sixth place where AI takes part, beyond the five ADR 0008 fixed, and it could widen a trigger to artefacts that no evidence covers.

## Consequences

- **ADR 0008 is unchanged.** Lessons add no AI step.
- **Generalisation is bounded.** Code rejects a generalisation whose root is a wildcard, or which fixes fewer than half of the fixed nodes in the smaller member's trigger. It runs within one scope only, never during the consistency pass, and only for fix kinds that are not tied to one artefact.
- **Members are superseded, not deleted.** Once a generalised lesson is confirmed, its members stop applying. If it is later revoked, they return to confirmed, each on its own evidence, and the retroactive search runs for them.
- **Wider lessons need a person.** A reviewer may add wildcards to a trigger by hand. The lesson then goes through the same gate.
- **A rule's own mistake still needs a tool release.** During a run, lessons change only what AI steps are given, or route a region to its faithful form or to manual work.
