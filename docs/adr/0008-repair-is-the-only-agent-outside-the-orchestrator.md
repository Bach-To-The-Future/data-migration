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

## Amendment, 17 October 2026: no narration of the report

The fifth place where AI takes part narrows from "narrating the report, with intent summaries" to **writing intent summaries** only. Both report documents are built from fixed sentence templates filled from the report data, so no model writes report prose. An intent summary appears in the client sign-off document only if a deterministic check passes: every table it names is in the artefact's read and write set, and its action verbs match the artefact's effects. Otherwise a template line built from the generated description is shown. The count of places stays at five. Decided in [What does the conversion report look like?](https://github.com/Bach-To-The-Future/data-migration/issues/9).

## Amendment, 18 October 2026: repair patches to deterministic regions

When repair fixes a region produced by deterministic lowering or generation rules, its patch is kept as a flagged **repair patch**, keyed by the hash of the region's inputs and reapplied on every regeneration while those inputs are unchanged. Repair also records an open problem for the maintainers, proposing a rule fix, in the engagement's control database. It is not a lesson, because lessons of that kind are global and hold no client code; maintainers fix the rule in a release (corrected after a second review, finding S4). A changed input drops the patch, and the region is converted and checked afresh. Decided in [Independent review of the assembled spec](https://github.com/Bach-To-The-Future/data-migration/issues/28), finding R3.

## Amendment, 7 November 2026: rules for promoting a module to an agent

Any module may be promoted from an LLM step to an agent only under five rules (owner input):

1. **It never decides** a verdict, a test, a threshold, or test data that decides a verdict.
2. **Its tools** are context-builder queries and checks on visible seeds only.
3. **Its full trajectory is recorded and replayed,** so a re-run gives identical delivered output.
4. **It has fixed attempt and budget limits,** versioned by the tool.
5. **It is promoted only if a benchmark arm shows it beats the LLM step it replaces.**

Rule 3 makes a re-run that replays the run catalog identical. A cold run without that record re-decides, as any agent does; the benchmark's stability guardrail measures how much that varies. Three candidate promotions are tested as benchmark arms in the build: the test-input step as an exploring agent, hard-case raising as a convert-check-retry agent, and an advisory code-quality agent choosing among candidates that all passed every check. Until an arm shows a gain, each stays as decided above.

## Amendment, 7 November 2026: after the architecture review

- **Promotion rule 2 is reworded:** an agent's tools are context-builder queries and checks on visible seeds only, except that an agent proposing test inputs may also run the original in the sandbox, never converted code (finding A4).
- **The three agent arms are post-core experiments,** off by default unless they win, and they do not block the orchestrator benchmark. Each has a success measure:
  - **(a) the exploring test-input agent:** mutants killed and coverage of the original gained, not converted share;
  - **(b) the convert-check-retry agent for hard raising:** the share of raised forms delivered, at equal converted share;
  - **(c) the advisory code-quality agent,** lowest priority: it chooses at the fixed picking rule's point, among candidates that passed layers 1 to 4, and is judged on a fixed maintainability score (helper calls, code length, nesting, flagged constructs) at equal converted share. It never changes a status.

Decided by the owner on [the architecture review](https://github.com/Bach-To-The-Future/data-migration/issues/35#issuecomment-6707883287).

## Amendment, 14 November 2026: after the agent-architecture review

Decided by the owner after an agent-architecture review on Claude Fable (14 November 2026). The count of places where AI takes part stays five.

**The promotion rules and the arms**

- **Rule 1 is reworded:** an agent never decides a verdict, a test or a threshold, and may add inputs to the visible set, never remove, replace or select hold-out data.
- **Arm (c), the advisory code-quality agent, is dropped.** Candidates are picked by the fixed picking rule. Why: a fixed maintainability score is one that code can maximise, so the agent could not lose; its choice decides which candidate meets the hold-out seeds, against "it never changes a status"; its flags would be model text in a report where no model writes prose; and it would be a sixth place where AI takes part, which ADR 0010 rejected.
- **Arm (a), the exploring test-input agent:** mutants killed is the deciding measure and coverage of the original gained is a guardrail, since coverage is the agent's own loop signal. Its sandbox runs are budgeted like any attempt. Its comparator is the step as it now is, a code-driven multi-round loop.
- **Arm (b), the convert-check-retry agent for hard raising,** is built only if the code-driven retry, which now carries the evidence of layers 3 and 4 on visible seeds, leaves room for it: the share of hard-case regions that still fall back to the faithful form must exceed the arm's minimum effect, because an agent cannot win by more than what is left to win.
- **Repair gets a third arm,** a fixed loop ([ADR 0020](0020-orchestrators-are-policies-over-a-fixed-engine.md), amended). Every arm states its minimum effect, variance test, guardrails and cost parity before it is built (ADR 0020).

**The LLM steps**

- **Hard raising:** a retry gets the checker's message and the evidence from layers 3 and 4 on visible seeds, not only the checker's message.
- **The generation fallback** produces 3 candidates, like lowering assist, checked and picked the same way; its output stays flagged as model-produced.
- **Proposing test inputs** is a code-driven multi-round loop: each round is one stateless call, and code recomputes the coverage gaps between rounds.
- **Intent summaries:** the deterministic check of 17 October is tightened. A summary is one line of at most 160 characters; every word comes from the artefact's own identifiers or from a fixed plain-English word list; it contains no assurance word (verified, safe, tested, and the like), since it is context and never a claim about the conversion.

**Repair**

- **A loop contract:** limits on patches proposed, visible-check runs and tool calls; a cap on each tool result; a no-progress stop (two successive patches that fail at the same first diverging checkpoint with the same difference); and an explicit `submit` of a patch that has passed the visible checks, the only way the model ends a run. Provisional values are in the spec's section 2.
- **A ceiling check between attempts:** a running repair run starts no further attempt once the cost ceiling is reached.
- **Hold-out disclosure rounds are capped per artefact** (provisional: 2). After that the artefact is manual work, with repair's evidence in the report.
- **Code rejects a patch that introduces a literal taken from the failing input,** before any check runs (the exact rule is in the spec's section 9). Each repair run is otherwise free to try again, so without this a patch could pass by fitting the input it was shown.
- **Repair changes the representation wherever the fix is expressible there.** Generated code is patched directly only where it is not. That is a **code-level patch**: flagged, listed in the report, the region's generated description marked as not generated from the representation, and it drafts no lesson (it is still kept as a repair patch, with an open problem for the maintainers).
- **The one-line hypothesis is evidence for reviewers only,** never rendered into any model's context ([ADR 0010](0010-lessons-are-drafted-and-generalised-by-code.md), amended).
- **The report gives the final-validator failure rate separately** for repaired artefacts and for artefacts delivered through an agent arm, beside the rate for the rest.

Decided by the owner, 14 November 2026.
