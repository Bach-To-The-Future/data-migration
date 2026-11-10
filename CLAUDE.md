## Standing rules

The owner's standing workflow rules for this repo. They override any skill's default.

1. **Work in a worktree on a short-lived branch, except for docs.** Each implementation session works in its own git worktree, on a branch named after its ticket (`ticket-41`, or a short name for a task with no ticket). When the ticket is done it rebases onto `master`, runs the full checks (`sh sandbox/engine-tests.sh`), pushes to `master` and deletes the branch and the worktree. **Commit and push straight to `master`, with no branch:** task and research output, `CONTEXT.md`, ADRs, `CLAUDE.md`, `CODING_STANDARDS.md`, `docs/agents/` and other changes that touch only docs. List each such change in the reply, so the owner can object after the fact. How parallel sessions share the sandbox is in [`docs/agents/worktrees.md`](docs/agents/worktrees.md).
2. **Write an ADR whenever a decision meets the bar:** hard to reverse, surprising without context, and the result of a real trade-off. All three, without asking first.
3. **Close an implemented ticket** once its acceptance criteria are met and its commits are pushed, with a summary comment: the commits, what was built, and anything deferred, naming the ticket that takes it. An implemented ticket left open blocks everything behind it in `tools/tickets.py`.
4. **Design check before finishing a ticket:** check the change against the spec sections it implements, every ADR it touches, and `CODING_STANDARDS.md`. List any knock-on changes to other tickets, the spec or ADRs, and record each where it belongs. If a ticket conflicts with the spec or an ADR, stop and ask. Background history: [the map's Notes, issue #1](https://github.com/Bach-To-The-Future/data-migration/issues/1).
5. **Docker:** Docker Desktop may be used for local tests on the owner's machine. Change Docker Desktop or WSL settings, or install or uninstall a container runtime, only when asked. The product itself never depends on Docker Desktop ([ADR 0017](docs/adr/0017-the-products-tools-are-free-and-open-source-where-possible.md)); CI proves it runs on Docker Engine.
6. **Write the tests first on every ticket (the /tdd loop):** turn the acceptance criteria into tests, run them and see them fail, then write the code. Probing an API to learn it does not replace this. Say in the reply if any part was not done test-first.

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
