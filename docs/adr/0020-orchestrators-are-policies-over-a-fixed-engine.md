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

- **`retry_up`.** The engine escalates inside a step: checker failures and failing candidates. The orchestrator sees only failures found after a whole step. Its `retry_up` re-runs the AI step behind the failing region one tier up. The engine refuses it for regions produced by deterministic rules, and at the top tier. The observation shows whether a failing region was produced by deterministic rules or by an AI step, and the baseline goes straight to repair for the former (added after a second review, finding S3).
- **When the ceiling binds.** It binds when projected remaining spend exceeds the remaining budget. From then on, `allocate` sets each artefact's maximum. The agentic orchestrator's own calls draw on a reserved share of the ceiling (5% by default, versioned), and when that is spent it falls back to the baseline's policy.
- **The agent's tier.** The agentic orchestrator's own calls run at the top tier, versioned with the tier table.
- **A split development corpus.** The development corpus is split deterministically into a tuning half and a benchmark half. The agent's prompt and tools are developed on the tuning half only, and the benchmark runs on the other. The release hold-out stays untouched. The cost: a smaller, noisier benchmark.
- **A double tie** on converted share and speed makes the baseline the default.
- **A repair arm.** One more arm runs 5 cold runs under the ample ceiling with the baseline orchestrator and repair replaced by a single fixed-prompt call. It decides whether repair moves down to an LLM step.
- **The benchmark needs the whole engine built** first.

## Amendment, 7 November 2026: after the architecture review

- **The benchmark runs on a shared result cache** (finding A3). The cache is populated from baseline cold runs; each orchestrator then runs 5 times against it, paying live model calls only for cache misses, which isolates orchestration. The stability guardrail is measured on 3 cold runs of each. The plain pipeline is the default until the benchmark runs; the benchmark then decides the default.
- **Deferred from v1** (finding A15): the `allocate` action, the agentic orchestrator's reserved budget share, and the binding-ceiling condition. Their trigger is the agentic orchestrator winning the benchmark. When the ceiling is reached, new AI steps stop in run-plan order.
- **The three agent arms** are post-core experiments and do not block the benchmark (ADR 0008).
- **The repair arm** uses the shared result cache for every step except repair itself, whose calls run cold in each of its 5 runs, so the arm isolates repair alone (owner's decision, 7 November 2026).

Decided by the owner on [the architecture review](https://github.com/Bach-To-The-Future/data-migration/issues/35#issuecomment-6707883287).

## Amendment, 14 November 2026: after the agent-architecture review

An agent-architecture review on Claude Fable (14 November 2026), made on the specification, these ADRs and the agent tickets, challenged each agent's design. The owner decided:

- **The agentic orchestrator is deferred from v1, and so is its benchmark.** v1 ships the plain pipeline. The policy interface, the actions, the engine's checks and the baseline stay, so the agentic orchestrator can be added later without changing the engine. Why: in v1 the engine routes everything to the top tier and refuses `retry_up` there, and budget allocation is deferred, so what is left for an orchestrator to choose (order, early give-up, requeueing) can barely move converted share. The benchmark would be nearly decided before it ran, at the cost of building the agent.
- **The trigger to revisit it** is both of: `retry_up` being real (tier calibration is done and some complexity band routes below the top tier) and budget allocation returning (a ceiling that binds in practice). Each alone leaves it too little to choose.
- **When it returns,** it comes with what section 11 of the spec now records: a cap on its calls and spend, with fallback to the baseline's decisions; a defined call trigger and a defined `wait`; a bounded observation; early `give_up` refused unless the ceiling is projected to bind, each one reported as an orchestrator decision.
- **The stability guardrail still applies.** The deferral here and in the 7 November amendment removes the orchestrator benchmark and the binding-ceiling condition, not the guardrail: status stability below 95% over 3 cold runs is flagged for every arm below, and for the agentic orchestrator when it returns.
- **The build's first experiment is now the repair arms.** Five cold runs each, with the baseline orchestrator and repair as the agent, as a **fixed loop** (the same number of attempts, with the evidence pre-fetched, each call stateless) and as a single fixed-prompt call. Repair stays an agent only if it beats the fixed loop; it becomes the fixed loop if that beats the single call; otherwise it is the single call. A tie goes to the simpler option. The repair arms still use the shared result cache for every step except repair, whose calls run cold in each run.
- **One rule decides every arm, and is stated before the arm is built.** Over 5 runs on the benchmark half, an arm wins only if its mean beats its comparator's by more than the larger of the two run-to-run ranges and by at least its minimum effect; otherwise it is a tie. The arm and its comparator get the same attempt and spend limits per artefact (cost parity), so a win cannot come from more tries. Guardrails, flagged and not deciding: the final-validator failure rate of the delivered output worse than the comparator's by more than the run-to-run range; status stability below 95%; cost per converted artefact above twice the comparator's. The comparators and the provisional minimum effects (the calibration ticket fixes them before the arm is built):
  - **repair,** against the fixed loop and against the single call: failures fixed, at least 2 artefacts;
  - **the test-input agent (a),** against the code-driven multi-round step: mutants killed, at least 3% more, with coverage of the original gained as a guardrail;
  - **the hard-raising agent (b),** against the code-driven retry that carries the evidence of layers 3 and 4: raised forms delivered at equal converted share, at least 2 raised forms.
  When the agentic orchestrator returns, the original decision's rule applies (more than the larger of the two ranges and at least 2 artefacts; speed second; a double tie to the baseline), with the same cost parity.
- **Orchestrator calls are excluded from the shared result cache.** The cache holds the baseline's step results. An orchestrator's own calls always run live in a benchmark and count in its cost; otherwise one side would pay for its calls and the other would not.
- **Worked examples in a benchmark run** exclude examples made from artefacts in the benchmark half of the development corpus, and an artefact is never shown an example made from itself (spec section 8). Without this a benchmark could reward remembering its own answers.
- **Lessons in a benchmark run:** Lessons drafted during a benchmark run may be used within that run and are discarded at its end, never entering the global lesson set; the global lessons in effect during a benchmark run are drafted only from artefacts in the tuning half of the development corpus (the sample corpus's among them), never from a benchmark-half artefact. A global lesson derived from a benchmark-half artefact would let the benchmark reward remembering its own answers, and lessons carried from one run into another would make the runs depend on each other.

Decided by the owner, 14 November 2026.
