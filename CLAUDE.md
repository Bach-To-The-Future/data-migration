## Agent skills

### Issue tracker

Issues and specs live in this repo's GitHub Issues, managed with the `gh` CLI. See `docs/agents/issue-tracker.md`.

Once an implemented ticket's acceptance criteria are met and its commits are pushed, close the issue with a summary comment: the commits, what was built, and anything deferred, naming the ticket that takes it. An implemented ticket left open blocks everything behind it in `tools/tickets.py`.

### Triage labels

The five canonical triage roles use their default label strings (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Engine

The engine is in `engine/`; see `engine/README.md`. Its lint, type checks and tests run only in the sandbox toolbox: `sh sandbox/engine-tests.sh`, which needs Docker running and `python corpus/build.py` done.
