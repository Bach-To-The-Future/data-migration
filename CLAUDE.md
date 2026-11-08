## Standing rules

The owner's standing workflow rules for this repo. They override any skill's default.

1. **Commit and push to `master` as soon as made, with no branch first:** task and research output, `CONTEXT.md`, ADRs, `CLAUDE.md`, `CODING_STANDARDS.md` and `docs/agents/`. List each such change in the reply, so the owner can object after the fact.
2. **Write an ADR whenever a decision meets the bar:** hard to reverse, surprising without context, and the result of a real trade-off. All three, without asking first.
3. **Close an implemented ticket** once its acceptance criteria are met and its commits are pushed, with a summary comment: the commits, what was built, and anything deferred, naming the ticket that takes it. An implemented ticket left open blocks everything behind it in `tools/tickets.py`.

The no-shortcuts rules for code (nothing degrades silently, "converted" means Reconciled and passed final validation, free and open-source tools) are in `CODING_STANDARDS.md`.

## Agent skills

### Issue tracker

Issues and specs live in this repo's GitHub Issues, managed with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles use their default label strings (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Engine

The engine is in `engine/`; see `engine/README.md`. Its lint, type checks and tests run only in the sandbox toolbox: `sh sandbox/engine-tests.sh`, which needs Docker running and `python corpus/build.py` done.
