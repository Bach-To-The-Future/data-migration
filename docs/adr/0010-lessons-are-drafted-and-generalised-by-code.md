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

## Amendment, 18 October 2026

- **"Trigger" is renamed "lesson pattern"**, so it is not confused with database triggers. The meaning is unchanged.
- **What "not by a model" means:** no model decides a lesson's pattern or its confirmation. Repair's one-line hypothesis, which a model writes, is stored in a client lesson as evidence and is dropped when a lesson is promoted to global.
- **Promotion also rejects any identifier from the client's catalog** (object, column, parameter and other names), wherever it appears in the lesson, beside the checks on literals and verbatim stretches.

Decided in [Independent review of the assembled spec](https://github.com/Bach-To-The-Future/data-migration/issues/28), finding R8 and Further Note 5.

## Amendment, 7 November 2026: after the architecture review

**Generalisation by anti-unification is deferred from v1**; its trigger is measured repeated near-identical repairs. **Promoting a client lesson to global is deferred**; its trigger is a second client engagement. v1 keeps exact lessons, reviewer wildcards and the confirmation gate (finding A18).

Decided by the owner on [the architecture review](https://github.com/Bach-To-The-Future/data-migration/issues/35#issuecomment-6707883287).

## Amendment, 14 November 2026: after the agent-architecture review

- **Repair's one-line hypothesis is never rendered into any model's context.** It is evidence for reviewers, kept in the repair record and shown in the report. A lesson no longer carries it, so a repair-drafted lesson's fix is the repaired region as a worked example, alone. This supersedes the 18 October amendment, which kept the hypothesis in a client lesson as evidence.
- **A lesson's worked example meets the worked-example pool's qualification rules** (spec section 8): the artefact it comes from is "Reconciled" with mutation testing run and passed, is not weakly tested, and does not rest on an accepted dependency. A lesson whose example does not qualify stays a candidate.
- **Lesson content is client-derived text,** so it goes in delimited data blocks like any other client text, never in the instructions.
- **The applicable-lessons slot has a cap** (provisional: 5 lessons per call, ordered by specificity, client lessons first when specificity is equal). A matched lesson that does not fit is recorded with the call and counted in the report, so a cap that bites can be seen. Within the cap the slot is still never trimmed.
- **A code-level repair patch drafts no lesson** (ADR 0008, amended).
- **Every lesson records the artefacts it was drafted from,** so that a lesson set can be built from, and checked against, a part of the corpus (the benchmark rule is in [ADR 0020](0020-orchestrators-are-policies-over-a-fixed-engine.md), amended).

Decided by the owner, 14 November 2026.
