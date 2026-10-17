---
status: accepted
---

# Delivered jobs hold a write lease on every table they write, and recover on start

Emulated transactions ([ADR 0002](0002-compensating-restore-transactions-by-default.md)), constraint enforcement ([ADR 0003](0003-write-wrapper-enforces-source-constraints.md)), and identity and row-version counters are correct only while nothing else writes the same table at the same time. That was stated as the **single-writer assumption**: "during a run, converted code is the only writer to the tables it writes". The independent review found two holes in it:

- It excluded writers outside the converted code, but not two converted jobs writing the same table at once. A rollback that restores a Delta version would then wipe out the other job's committed writes.
- "During a run" means nothing in production, where there is no tool run.

We decided:

- **The assumption is restated:** at most one writer per table at any moment, converted or not, across every project in the environment.
- **Enforcement by lease.** Each delivered job takes a lease on every table it writes before its first write, recorded in the support library's state table. A second converted writer that finds a live lease fails at once with a clear error naming the table and the holder, rather than proceeding. Writers outside the converted code cannot be stopped this way, and the restated assumption still binds them.
- **Recovery on start.** Delivered session state records each transaction's start versions and every scratch table in the same state table. At the start of every run it checks for unfinished transactions and scratch tables left by a crash, and recovers them before any other work: it restores the recorded versions, drops the scratch tables and releases the leases. The recovery command still ships in the project, for manual use.

Both are stated in the client sign-off document with the restated assumption.

We chose this because silent data loss under concurrency is worse than a failed job, and because the run catalog that recovers crashed runs during testing does not exist in production.

The full decision is in the resolution of [Independent review of the assembled spec](https://github.com/Bach-To-The-Future/data-migration/issues/28), findings R1 and R4.

## Considered options

- **State the restated assumption only, without enforcement.** Simpler. Rejected: a client's scheduler can start two jobs on the same table by accident, and the cost is silent loss of committed data.
- **A recovery command run by hand after a crash.** Rejected as the main route: a half-applied transaction would stay until someone noticed. The command is kept for manual use.

## Consequences

- **Converted jobs that share a table cannot run in parallel.** The second fails fast. Clients must schedule them serially.
- **The state table holds counters, leases, transaction start versions and scratch registrations.** Its schema is shared by every library version and is only ever extended ([ADR 0018](0018-the-support-library-ships-as-source-vendored-per-project.md)).
- **A job that dies holds its leases** until the next run's start-up recovery releases them, or an operator runs the recovery command.
- **The testing module and the final validator exercise the lease and the recovery,** as they exercise the counter initialisation command.
