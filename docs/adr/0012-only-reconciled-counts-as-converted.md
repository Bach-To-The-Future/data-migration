---
status: accepted
---

# Only "Reconciled" counts as converted, and nothing degrades silently

Reconciliation is an optional stage. The earlier rule made it a gate only when it was enabled, so with it disabled an artefact could be called converted on the strength of static checks alone. We decided that nothing is reported as converted unless its verdict is "Reconciled", whether reconciliation is enabled or not. Reconciliation is on by default. A run with it disabled is a **preview run**: its report says so on its first line, and every artefact is reported as "Not reconciled", never as converted.

With this goes a rule that the tool never degrades silently. Every skipped check, fallback, assumption, trimmed context, unreachable branch and reduction in coverage is recorded and shown in the report. No component may make a check pass by weakening it.

We chose this because the report is what a client trusts. A conversion that was never run against the original has not been shown to be correct, and a quiet fallback hides exactly the cases a reviewer most needs to see.

The owner set this as a standing rule for the map on 16 October 2026; see the Notes of [the map](https://github.com/Bach-To-The-Future/data-migration/issues/1).

## Considered options

- **Converted on static checks when reconciliation is disabled.** It gives a run without a sandbox a usable headline figure. Rejected: it would let the same word mean "proven equal on data" in one run and "parses and type-checks" in another.

## Consequences

- **A preview run converts nothing.** It still produces the faithful form, the static checks and the full report, with every artefact "Not reconciled" and the first line of the report saying it is a preview run. No raised form is delivered in it.
- **A preview run learns nothing it can confirm.** The confirmation gate needs "Reconciled", so lessons drafted in a preview run stay candidates, recorded for the next full run, and the bulk waits only for every pilot artefact to have a verdict. Nothing from a preview run becomes a worked example.
- **"Converted" has one meaning everywhere:** the share of artefacts whose verdict is "Reconciled". Every other status is reported under its own name: weakly tested, not reconcilable locally, blocked, failed, not reconciled.
- **The directional orchestrator prototype** checks statically only, so it reports "passed static check", never "converted".
- **Every degradation needs a place in the report.** A component that cannot record one may not degrade at all.

## Amendment, 16 October 2026

"Converted" now also requires passing final validation: an artefact counts as converted only if its verdict is "Reconciled" **and** it passes the independent final validator. "Failed final validation" is one more status reported under its own name. See [ADR 0013](0013-an-independent-final-validator.md).

## Amendment, 18 October 2026: artefacts that differ after an error

An artefact converted under [ADR 0004](0004-stop-at-first-error-unless-at-at-error-is-read.md), stopping where the original carried on after an error, still counts as converted if it reconciles and passes final validation. It carries a flag shown in the client sign-off document beside the item: "differs from the original after an error". The sign-off's headline sentence adds "except where flagged". Decided in [Independent review of the assembled spec](https://github.com/Bach-To-The-Future/data-migration/issues/28), finding R5.
