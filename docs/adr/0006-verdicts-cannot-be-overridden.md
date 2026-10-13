---
status: accepted
---

# A verdict cannot be overridden, only accompanied by a recorded acceptance

The product is sold on evidence: a client is shown what was checked and what the checks found. We decided that nobody can change a verdict, neither an AI step nor a person. An operator who decides to accept an artefact that is not reconciled attaches a recorded acceptance, with their name and reason, which the report shows beside the verdict and never in place of it. We chose this over an override because a verdict that can be edited is no longer evidence of what the tests found.

The full decision is in [the resolution of the ticket that made it](https://github.com/Bach-To-The-Future/data-migration/issues/20#issuecomment-6142928591).

## Considered options

- **Letting an operator overrule a verdict.** Simpler for an engagement under time pressure, and what most test tooling allows through skips and waivers. Rejected: the report would then mix what the tests found with what someone decided, and a reader could not tell them apart.

## Consequences

- **A human decision is still possible, and visible.** Accepting an artefact that failed or was weakly tested is a recorded act with a name on it.
- **The numbers stay honest.** Counts of reconciled artefacts in the report and in any acceptance criteria are never inflated by acceptances.
- **The only way to change a verdict is to change what is tested.** The artefact is converted again, or the tool is corrected, and the checks run again from the first layer.
- **Verdicts are tied to their test definition.** Each records a fingerprint of the generator, seeds, comparison rules, tolerances, mutation operators and thresholds, and is invalid if that does not match the tool's pinned definition.
