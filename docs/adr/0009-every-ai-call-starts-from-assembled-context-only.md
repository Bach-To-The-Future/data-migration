---
status: accepted
---

# Every AI call starts from assembled context only

An AI step could be run as a conversation that keeps what it learnt about an artefact, or about the client's code base, from one call to the next. We decided that every model call is stateless: it starts only from the context the context builder assembled for it, and no conversation history carries over between steps, artefacts or candidates. The one exception is a repair run, whose context grows during its work on one failure and is discarded when that failure is settled. Each assembled context is recorded, and the cache of model results lives in the engagement's control database, never shared with another engagement. We chose this because a call that depends only on its recorded inputs can be cached, replayed and audited, and because one client's code then cannot reach another client's results through the model or the cache.

The full decision is in [the resolution of the ticket that made it](https://github.com/Bach-To-The-Future/data-migration/issues/21#issuecomment-6210945499).

## Considered options

- **A conversation per artefact**, carrying what earlier steps saw into later ones. It would save tokens and might give the model a fuller picture. Rejected: what a step was shown would then depend on the order of earlier calls, so its result could neither be cached on its inputs nor replayed from the run catalog.
- **A cache shared across engagements.** Identical inputs from different clients would be paid for once. Rejected: a cache hit would hand one client output produced from another client's code, and deleting a client's data would no longer mean deleting their control database.

## Consequences

- **Everything a step needs must be in its assembled context.** A step that keeps failing for lack of something is fixed in the context builder, not by giving the step memory.
- **Repair's growth stays bounded and recorded.** Every query a repair run makes is served by the context builder under the same withholding rules and written to the run catalog.
- **The cache key is the hash of the rendered context** plus the prompt version, model version, candidate index with its context variant, and attempt number. The rendered context is stored compressed beside it.
- **Work is repeated across clients.** Two engagements with the same artefact pay for its model calls twice.
