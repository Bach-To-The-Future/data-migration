---
status: accepted
---

# Only "Reconciled" counts as converted, and nothing degrades silently

Reconciliation is an optional stage. The earlier rule made it a gate only when it was enabled, so with it disabled an artefact could be called converted on the strength of static checks alone. We decided that nothing is reported as converted unless its verdict is "Reconciled", whether reconciliation is enabled or not. With reconciliation disabled, every artefact is reported as "Not reconciled", never as converted.

With this goes a rule that the tool never degrades silently. Every skipped check, fallback, assumption, trimmed context, unreachable branch and reduction in coverage is recorded and shown in the report. No component may make a check pass by weakening it.

We chose this because the report is what a client trusts. A conversion that was never run against the original has not been shown to be correct, and a quiet fallback hides exactly the cases a reviewer most needs to see.

The owner set this as a standing rule for the map on 16 October 2026; see the Notes of [the map](https://github.com/Bach-To-The-Future/data-migration/issues/1).

## Considered options

- **Converted on static checks when reconciliation is disabled.** It gives a run without a sandbox a usable headline figure. Rejected: it would let the same word mean "proven equal on data" in one run and "parses and type-checks" in another.

## Consequences

- **A run with reconciliation disabled converts nothing.** It still produces the faithful form, the static checks and the full report, with every artefact "Not reconciled".
- **"Converted" has one meaning everywhere:** the share of artefacts whose verdict is "Reconciled". Every other status is reported under its own name: weakly tested, not reconcilable locally, blocked, failed, not reconciled.
- **The directional orchestrator prototype** checks statically only, so it reports "passed static check", never "converted".
- **Every degradation needs a place in the report.** A component that cannot record one may not degrade at all.
