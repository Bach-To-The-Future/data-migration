# Working in parallel: worktrees and the shared sandbox

Several sessions may implement different tickets at once. Each works in its own git worktree, and all of them share one sandbox: one SQL Server and one Postgres server, started once by `python corpus/build.py`. Standing rule 1 in [`CLAUDE.md`](../../CLAUDE.md) sets the workflow. This note holds the mechanics.

## The workflow

1. From the main checkout, `git worktree add ../data-migration-wt-<ticket> -b ticket-<number> master`.
2. Copy `sandbox/.env` into the worktree's `sandbox/`. It is not tracked, and the compose file reads it.
3. Work there, tests first. Run the checks with `sh sandbox/engine-tests.sh` (arguments go to pytest). The script mounts the worktree it sits in, and leaves a running sandbox alone (`--no-deps`): without that, compose would recreate the shared SQL Server because its configuration, which names the worktree's paths, differs.
4. When the ticket is done: `git fetch && git rebase origin/master`, run the full checks again, `git push origin HEAD:master`, then `git worktree remove ../data-migration-wt-<ticket>` and `git branch -d ticket-<number>`.
5. Docs-only changes (the files named in rule 1) may go straight to `master` from any checkout.

A rebase can conflict with another session that changed the same file. Resolve it, and run the checks again after, not before.

## Names that never collide

Every scratch database, schema and engagement created by the tests or the engine carries a per-run suffix, so one run never drops or reads another's.

- **Tests.** `conftest.RUN` is eight hexadecimal characters, new for each pytest process (`MIGRT_TEST_RUN` fixes it, for a test that starts runs of its own). The suite's scratch databases are `MigrtExtractTest_<RUN>` (`conftest.SCRATCH`) and `MigrtShapeTest_<RUN>`; engagements, and so their Postgres control databases, are `test_<RUN>_<unique>`; logins are `t_<unique>`. Tests refer to a scratch database by the variable, never by a literal name. A new test that creates a database, schema, login or engagement names it from `RUN` or a fresh UUID.
- **The engine.** The sandbox's scratch databases for result shapes are `migrt_sandbox_<label>_<unique>`. `engine.Sandbox(..., label=...)` sets the label; the tests pass `RUN`, and a test that looks for its own leftovers filters on it.
- **Corpus databases** (`RetailSample`, `AdventureWorks2022`, `WideWorldImporters`) are shared and read-only for tests. A test that changes one builds a scratch copy instead.

`engine/tests/test_parallel_runs.py` runs two pytest processes at once against the one sandbox, and two descriptions at once, and fails if either touches the other's objects.

## What a crashed run leaves

A run that is killed leaves its databases behind: `SELECT name FROM sys.databases WHERE name LIKE '%[_]<RUN>'` and `migrt[_]sandbox[_]%`. Drop the ones whose run is no longer going. Leftovers do not affect other runs.
