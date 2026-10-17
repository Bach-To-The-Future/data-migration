---
status: accepted
---

# Orchestrators are interchangeable policies over a fixed engine

The owner's hypothesis is that an agentic orchestrator, a model in a loop choosing its own steps, converts more than a plain pipeline. That has to be tested, so both must drive the same components unchanged, and the comparison must measure orchestration alone. We decided that an orchestrator is a **policy**: it sees an observation of the run and returns actions, which the engine runs, checks and records. Everything else is fixed by the engine, the same for both:

- the run plan, its phases and partial order;
- the fixed sequence of steps for each artefact;
- attempt limits, budgets per artefact and the run's cost ceiling;
- model routing and escalation;
- candidates;
- repair's own loop;
- the final validator's call points, the consistency pass and lesson drafting;
- the parallelism setting.

**An orchestrator chooses only:**

- the order of ready items;
- on a failed check, whether to retry one tier up, send the item to repair, or give up;
- giving up early;
- requeueing an item it gave up early, or a blocked one, when new information arrives;
- how to spend the remaining budget when the ceiling binds.

**It never skips a step.** It sees statuses, failure layers and dimensions, attempts, complexity scores, cluster and pattern identifiers, costs and budget left. It never sees source text, generated code, failing inputs, seeds or the validator's evidence.

We chose this because a narrow, explicit choice is the only fair way to compare a model's judgement with code's. A wider choice, such as skipping raising, would let an orchestrator win on speed at a cost that "converted" doesn't measure.

The directional prototype planned for this decision was dropped by the owner: its setup differed too much from the product to give trustworthy evidence. The benchmark is the build's first experiment instead.

The full decision is in [the resolution of the ticket that made it](https://github.com/Bach-To-The-Future/data-migration/issues/15#issuecomment-6237311379).

## Considered options

- **Let the agent drive the components directly, with free choice of steps.** That is closest to the hypothesis as first stated. Rejected: it would make the two orchestrators' outputs incomparable, and ADR 0008 already fixes each component's level.
- **A directional prototype before the build.** Rejected by the owner: a whole-procedure LLM conversion over a stub library would have measured something other than the product.

## Consequences

- **The benchmark compares orchestration alone.** It runs 5 cold runs of each orchestrator under two conditions: an ample ceiling, and a ceiling binding at 60% of the baseline's spend. It uses the development corpus, never the release hold-out.
  - **Deciding:** the converted share decides first, and speed second.
  - **Guardrails:** cost and stability.
  - **The winner:** the winner under the ample ceiling becomes the default.
- **Both orchestrators stay in the product** behind one switch. After the pilot, if projected spend exceeds the ceiling, the tool warns the operator and points to the switch's documented recommendation. It never switches by itself.
- **Every action is recorded twice,** as intent and then as result, and steps are idempotent. An interrupted run resumes by replaying its recorded decisions.

## Amendment, 18 October 2026

Decided in [Independent review of the assembled spec](https://github.com/Bach-To-The-Future/data-migration/issues/28), findings R2, R9, R10, R17 and Further Note 6.

- **`retry_up`.** The engine escalates inside a step: checker failures and failing candidates. The orchestrator sees only failures found after a whole step. Its `retry_up` re-runs the AI step behind the failing region one tier up. The engine refuses it for regions produced by deterministic rules, and at the top tier.
- **When the ceiling binds.** It binds when projected remaining spend exceeds the remaining budget. From then on, `allocate` sets each artefact's maximum. The agentic orchestrator's own calls draw on a reserved share of the ceiling (5% by default, versioned), and when that is spent it falls back to the baseline's policy.
- **The agent's tier.** The agentic orchestrator's own calls run at the top tier, versioned with the tier table.
- **A split development corpus.** The development corpus is split deterministically into a tuning half and a benchmark half. The agent's prompt and tools are developed on the tuning half only, and the benchmark runs on the other. The release hold-out stays untouched. The cost: a smaller, noisier benchmark.
- **A double tie** on converted share and speed makes the baseline the default.
- **A repair arm.** One more arm runs 5 cold runs under the ample ceiling with the baseline orchestrator and repair replaced by a single fixed-prompt call. It decides whether repair moves down to an LLM step.
- **The benchmark needs the whole engine built** first.
