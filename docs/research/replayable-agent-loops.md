# What makes an agent loop checkpointed and replayable?

Research for [issue #5](https://github.com/Bach-To-The-Future/data-migration/issues/5). Sources read on 2026-10-13. Primary sources only: Anthropic's documentation for the Claude API and the Claude Agent SDK, and the official documentation of LangGraph, Temporal, DBOS and Pydantic AI. Nothing here was verified by running code; this is a documentation study for a design that is not yet built.

Sentences marked **Inference** are conclusions drawn from the cited facts, not statements made by a source.

## Answer

### The one mechanism every source agrees on

A model's output cannot be regenerated; it can only be recorded and read back. Anthropic states that "even with temperature set to 0, the results will not be fully deterministic and identical inputs may produce different outputs across API calls" ([glossary][gloss]), and on Claude Opus 4.7 and later, including Claude Opus 5.5, a non-default `temperature`, `top_p` or `top_k` is rejected with a 400 error ([Opus 5.5 migration guide][mig55]).

Every durable-execution framework examined therefore makes an agent loop replayable the same way: the loop is ordinary deterministic code, and each model call and each tool call is a recorded step whose stored result is returned on replay instead of the step running again.

- Temporal: "To handle non-deterministic operations like API calls, LLM/AI invocations, database queries, and other external interactions, put them in Activities" ([Workflow Definition][t-wf]); "completed Activities will not re-execute as part of a Workflow Replay" ([Activity Definition][t-act]).
- LangGraph: "Nodes before the checkpoint are skipped (their results are already saved)" ([Checkpointers][lg-cp]); a task's result "will be loaded from the checkpoint instead of being recomputed" ([Functional API][lg-fn]).
- DBOS: "a workflow function must be deterministic", non-deterministic operations go in steps, and "Steps are tried at least once but are never re-executed after they complete" ([DBOS workflows][dbos]).
- Pydantic AI: "Each engine wraps every model request and tool call as its own durable unit" ([Pydantic AI durable execution][pyd]).

Temporal's own account of why a model-driven loop still satisfies its determinism rule is the clearest statement of the pattern: "given the same sequence of LLM responses, the Workflow will always execute the same way", and on replay "the Workflow does not ask the agent for a new plan for decisions it has already made" ([Temporal blog, 2025-11-12][t-blog]).

**Inference:** an agentic orchestrator can be resumable and replayable, but only in the sense "replay the decisions this run already recorded". It is not reproducible: a second fresh run over the same artefact may choose different steps.

### What the run catalog must record

**Inference** from the sources cited in each row. Per artefact, as an ordered, append-only log:

| Record | Why it is needed | Source of the requirement |
| --- | --- | --- |
| Content hash of the artefact and of every other input a step read (table shapes, referenced artefacts) | A recorded decision is only valid for the input it was made on; a changed input must be detectable | LangGraph replays re-execute everything after the chosen checkpoint ([time travel][lg-tt]); Temporal fails with a non-determinism error when replay diverges from history ([Workflow Definition][t-wf]) |
| The model ID as sent, and the `model` field of the response | Every model ID "identifies a pinned version of the model", so the ID is the model version; the response can come from a different model when a fallback is configured | [Model IDs and versioning][mid]; `fallback_model` in the [Agent SDK Python reference][sdk-py] |
| Prompt version: system prompt, tool definitions in order, effort, thinking settings, `max_tokens`, beta headers | These are all inputs to the model call; tools, system and messages form the prompt in that order | [Prompt caching][pc] |
| For each model call: the request (or a canonical hash of it), and the full response content exactly as returned, including `tool_use` blocks (`id`, `name`, `input`) and thinking blocks | The response is the decision. Thinking blocks must be passed back unchanged, and the conversation must stay append-only, or later requests are rejected | [Handle tool calls][tool]; [Opus 5.5 migration guide][mig55] |
| `stop_reason` and `usage` of each call | Distinguishes a finished step from a truncated, refused or paused one; gives cost per step | [Using the Messages API][msg]; [Batch processing][batch] (`pause_turn`) |
| For each tool call: `tool_use_id`, name, input, the result exactly as returned to the model, `is_error`, and a started/completed marker | Replay returns the recorded result; a step that started but was not recorded as completed will run again | [Handle tool calls][tool]; [Activity Definition][t-act]; [Functional API][lg-fn] |
| An idempotency key for every tool call with a side effect (file written, row inserted) | No framework gives exactly-once execution of a side effect; they give at-least-once plus recorded completion | [Activity Definition][t-act]; [Functional API][lg-fn]; [DBOS workflows][dbos] |
| Step status and attempt count, with the error of a failed attempt | Needed to resume at the first unfinished step | [Checkpointers][lg-cp] (pending writes); [Fault tolerance][lg-ft] |
| For batched calls: the batch ID and the mapping from `custom_id` to (artefact, step) | Results come back in any order and are matched only by `custom_id`; results are downloadable for 29 days | [Batch processing][batch] |
| Content hash of every produced file and of the conversion report | Lets a re-run confirm that the recorded output is still the one on disk | Agent SDK sessions "persist the conversation, not the filesystem" ([Sessions][sdk-sess]) |
| Any compaction summary that replaced earlier history | A summary is itself a model output; after compaction the transcript the model sees is not the raw log | [Agent loop][sdk-loop]; [Session storage][sdk-store] |

The cache key the owner proposed (input + prompt version + model version) is sound as far as the model version goes: "Anthropic does not update the weights or configuration of an existing model ID. When an updated version is available, it ships under a new model ID" ([Model IDs and versioning][mid]). **Inference:** for an agent step the "input" part of the key is the whole conversation so far, that is, every earlier decision and tool result for that artefact, so a change early in an artefact's history invalidates every later cached step for it.

### What cannot be made replayable

1. **A fresh model decision.** Identical inputs may produce different outputs ([glossary][gloss]); sampling parameters are rejected on current models ([Opus 5.5 migration guide][mig55]). Even on one model ID, "infrastructure updates produce minor differences in observable behavior even when the model ID and weights have not changed" ([Model IDs and versioning][mid]).
2. **Anything after a divergence.** Once an input, a tool result or the model changes, the steps after that point are new decisions. LangGraph says so directly: "Replay re-executes nodes—it doesn't just read from cache" and results "may return different results" ([time travel][lg-tt]). **Inference:** an agent may then take a different number of steps or call different tools, so the old log cannot be matched step for step against the new run.
3. **Exactly-once side effects.** "Because Activities may be retried, these functions may be executed more than once" ([Activity Definition][t-act]); "A task that started but did not finish may run again on that resume" ([Functional API][lg-fn]). Only idempotent tools make this harmless.
4. **A recorded conversation continued on a different model.** Thinking blocks are tied to the model that produced them: only Claude Fable 5.1 and Claude Mythos 5.1 read Claude Opus 5.5 thinking blocks, and a router that moves a conversation to another model "runs those turns without them" ([Opus 5.5 migration guide][mig55]). Prompt caches are also scoped to the model ([Prompt caching][pc]).
5. **A recorded conversation whose earlier turns were edited.** For accounts created on or after 2026-08-31, replaying a thinking block after an edit to the system prompt, tools or earlier messages returns a 400 error by default ([Opus 5.5 migration guide][mig55]). The catalog must be append-only for this reason as well.
6. **Provider-side state.** A prompt cache entry lives 5 minutes, or 1 hour at extra cost ([Prompt caching][pc]); batch results are available for 29 days and a batch expires if not processed within 24 hours ([Batch processing][batch]); a retired model stops answering, with at least 60 days' notice ([Model deprecations][dep]). Steps already recorded still replay after all of these; nothing new can be produced under the old key once the model is retired.
7. **The model's reasoning.** On Claude Opus 4.7 and later, thinking blocks are returned with an empty `thinking` field unless the caller opts in to summarised thinking ([Opus 5.5 migration guide][mig55]). **Inference:** the catalog can store what was decided and, at most, a summary of why.

## Claude API

### Determinism and model versions

- Not deterministic at any setting, on Anthropic's own service and on third-party cloud providers ([glossary][gloss]).
- "If you were using `temperature = 0` for determinism, note that it never guaranteed identical outputs on prior models" ([Opus 5.5 migration guide][mig55]).
- No seed or reproducibility parameter appears in any page read for this ticket. Its absence from the full API reference was not checked exhaustively.
- Model IDs are pinned snapshots, including the dateless IDs used since the 4.6 generation; `claude-opus-5-5` "is not an alias. It is the snapshot" ([Model IDs and versioning][mid], [Models overview][models]).
- Current IDs: `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`, `claude-fable-5-1`; all with a 1M-token context window and 128K maximum output ([Models overview][models]).
- Retirement: `claude-opus-5-5` not sooner than 2027-09-22, `claude-sonnet-5-5` not sooner than 2027-09-28, `claude-haiku-5-5` not sooner than 2027-10-07; "Requests to retired models will fail" ([Model deprecations][dep]).
- On Claude Opus 5.5, adaptive thinking is always on ([Models overview][models]) and a forced `tool_choice` (`any` or a named tool) is rejected ([Opus 5.5 migration guide][mig55]). **Inference:** a design that planned to force a specific tool call to obtain a structured decision must use `tool_choice: auto` with strict tools or structured outputs instead.

### Tool-call records and resuming a conversation

- "The Messages API is stateless, which means that you always send the full conversational history to the API", and "Earlier conversational turns don't necessarily need to actually originate from Claude" ([Using the Messages API][msg]). **Inference:** a recorded transcript can be sent again to continue from any recorded turn; the API holds no session that could be lost.
- A tool call arrives as a `tool_use` block with `id`, `name` and `input`; the reply is a `tool_result` block carrying the same `tool_use_id`, optional `content` and optional `is_error`; "Tool result blocks must immediately follow their corresponding tool use blocks in the message history" ([Handle tool calls][tool]).
- `strict: true` on a tool definition "guarantees that tool inputs will always match your schema exactly" ([Handle tool calls][tool]).
- Whether the Messages API accepts an idempotency key, so that a retried request after a lost response is not billed and decided twice, could not be established: no first-party page describing one was found.

### Prompt caching is not a result cache

- It caches the prompt prefix (`tools`, then `system`, then `messages`, up to a marked block), and "has no effect on output token generation. The response you receive is identical to what you would get if prompt caching were not used" ([Prompt caching][pc]). The model still generates a new answer on every call.
- "Cache hits require 100% identical prompt segments" ([Prompt caching][pc]).
- Lifetime is 5 minutes by default, refreshed at no cost on each use, with a 1-hour option at extra cost; measured from the start of the request ([Prompt caching][pc]).
- Isolated per workspace on the Claude API; never shared between organisations ([Prompt caching][pc]).
- Minimum cacheable prefix is 512 tokens on Claude Opus 5.5, Sonnet 5.5 and Haiku 5.5 ([Prompt caching][pc]).
- "a cache entry only becomes available after the first response begins", so parallel requests sharing a prefix all miss unless the first is allowed to start first ([Prompt caching][pc]).

**Inference:** the "LLM results cached on input + prompt version + model version" requirement has to be met by the tool's own store. Prompt caching only lowers the price and the rate-limit weight of a call that is actually made.

### Message Batches API

All from [Batch processing][batch] unless noted.

- Up to 100,000 requests or 256 MB per batch; "most batches completing within 1 hour"; results are available when all requests finish or after 24 hours; unprocessed requests expire at 24 hours.
- Results are kept 29 days from creation, "can be returned in any order", and are matched with `custom_id` (1 to 64 characters, letters, digits, hyphen, underscore).
- Each request ends as `succeeded`, `errored`, `canceled` or `expired`; only `succeeded` is billed; "the failure of one request in a batch does not affect the processing of other requests".
- A submitted batch cannot be modified, only cancelled.
- Tool use, multi-turn conversations, thinking and most beta features are supported; `stream: true`, `speed` and `max_tokens: 0` are not.
- Prompt caching works in a batch and the discounts stack, but hits are "best-effort", typically "30% to 98%"; Anthropic suggests the 1-hour cache for batches.
- A batch result can end with `stop_reason: "pause_turn"`, meaning the turn did not finish and must be continued in a follow-up request.
- Batches are scoped to a workspace and "may go slightly over your Workspace's configured spend limit".

**Inference:** a batch returns one model turn per request, hours later at worst. An agent loop needs each turn's tool results before it can ask for the next turn, so a loop of K turns per artefact costs K batch round trips. Batching fits a run that advances all artefacts one step at a time, and fits poorly a run that drives one artefact's loop to completion before starting the next.

## Claude Agent SDK

The Agent SDK runs "the same execution loop that powers Claude Code": both SDKs "bundle a native Claude Code binary" ([Agent loop][sdk-loop]), and the loop runs in that subprocess, which writes the session transcript ([Session storage][sdk-store]).

### Sessions: persistence, resume, fork

- A session "contains your prompt, every tool call the agent made, every tool result, and every response. The SDK writes it to disk automatically" as JSONL under `~/.claude/projects/<encoded-cwd>/` ([Sessions][sdk-sess]).
- `resume` takes a session ID; `continue_conversation` takes the most recent session in the directory; `fork_session` copies the history into a new session; `resume_session_at` loads the conversation only up to a given message UUID ([Sessions][sdk-sess], [Python reference][sdk-py]).
- The session ID is on every result message, success or error; a connection or process failure yields no result message ([Sessions][sdk-sess]).
- "Sessions persist the **conversation**, not the filesystem" ([Sessions][sdk-sess]).
- Session files are local to the machine. A `SessionStore` adapter mirrors them to your own storage, but "Mirror writes are best-effort": after three failed attempts the batch is dropped and the query continues; retried batches can deliver duplicates, to be removed by `entry.uuid` ([Session storage][sdk-store]).
- Anthropic's own advice for moving work between hosts includes: "Don't rely on session resume. Capture the results you need (analysis output, decisions, file diffs) as application state and pass them into a fresh session's prompt. This is often more robust than shipping transcript files around" ([Sessions][sdk-sess]).
- Automatic compaction "replaces older messages with a summary"; `get_session_messages` returns the post-compaction chain, while the raw history is only available from the store ([Agent loop][sdk-loop], [Session storage][sdk-store]).
- The transcript file "is written asynchronously and may lag the in-memory conversation" ([Hooks reference][cc-hooks]).

Resume restores context so the agent can continue; none of the pages read describes it as a replay that re-drives recorded steps. What a resumed session does about a tool call that was in flight when the process died is not documented in the pages read.

### File checkpointing

- Tracks only changes made through the Write, Edit and NotebookEdit tools; "Changes made through Bash commands ... are not captured", nor are subagent edits ([File checkpointing][sdk-fcp]).
- Rewinding "restores files on disk to a previous state. It does not rewind the conversation itself" ([File checkpointing][sdk-fcp]).
- Checkpoints are tied to the session that created them, cover local files only, and do not undo directory creation, moves or deletes ([File checkpointing][sdk-fcp]).
- It cannot be combined with a `SessionStore`: the SDK throws at startup ([Session storage][sdk-store]).

### Hooks as the recording seam

- `PreToolUse` receives the tool name and input and can allow, deny, ask, defer, or rewrite the input; `PostToolUse` can replace the tool's output with `updatedToolOutput`, for any tool in both SDKs ([Hooks][sdk-hooks]).
- Returning `defer` ends the turn with `stop_reason` `tool_deferred` so the call can be resumed later ([Hooks][sdk-hooks]).
- A tool-use ID "correlates `PreToolUse` and `PostToolUse` events for the same tool call" ([Hooks][sdk-hooks]).
- "Hooks may not fire when the agent hits the `max_turns` limit" ([Hooks][sdk-hooks]).

No hook described in the pages read returns a recorded result in place of running a built-in tool: a denial gives the model a rejection message, and `updatedToolOutput` applies after the tool has run. **Inference:** with the Agent SDK, a recorded tool result can be substituted only inside custom tools whose handlers the tool itself owns; for built-in tools an Agent SDK session is one opaque catalog step, replayed by skipping the whole session when its recorded output is present.

### Limits and cost reporting

- `max_turns` caps tool-use round trips; `max_budget_usd` stops at an estimated spend, which can be exceeded by the cost of one response; the result subtype is `error_max_turns` or `error_max_budget_usd`, and the session can be resumed with a higher limit ([Agent loop][sdk-loop]).
- `total_cost_usd` is a "client-side" estimate, "not authoritative billing data" ([Cost tracking][sdk-cost]).
- The SDK applies prompt caching automatically ([Cost tracking][sdk-cost]).
- No Agent SDK page in the documentation index mentions the Message Batches API ([docs index][cc-index]). Whether the SDK can route its model calls through a batch could not be established.

## LangGraph

- **Checkpoints.** A checkpointer saves a thread's state; a thread ID is required. "LangGraph creates a checkpoint at each super-step boundary", and time travel only resumes from those boundaries ([Checkpointers][lg-cp]). A super-step is "a single iteration over the graph nodes"; nodes that run in parallel share one ([Graph API][lg-graph]).
- **Partial failure.** When a node fails mid super-step, "LangGraph stores pending checkpoint writes from any other nodes that completed successfully", and on resume "you don't re-run the successful nodes" ([Checkpointers][lg-cp]).
- **Replay.** Invoking with an earlier checkpoint ID skips nodes before it and re-executes nodes after it, "including any LLM calls, API requests" ([Checkpointers][lg-cp]). "Replay re-executes nodes—it doesn't just read from cache" ([time travel][lg-tt]).
- **Fork.** `update_state` "creates a new checkpoint that branches from the specified point"; "The original execution history remains intact" ([time travel][lg-tt]).
- **Durability modes.** `exit` persists only when the graph exits, so intermediate state is lost on a crash; `async` persists while the next step runs and may miss a checkpoint on a crash; `sync` persists before the next step starts, at some performance cost ([Checkpointers][lg-cp]). The default mode was not captured.
- **Constraints on agent code.** Non-deterministic work and side effects must sit inside tasks or nodes; "resuming a specific thread should replay the same persisted task and subgraph results"; side effects must be idempotent because an unfinished task may run again; task outputs and entrypoint inputs and outputs "must be JSON-serializable" ([Functional API][lg-fn]).
- **Backends.** In-memory, SQLite and Postgres ("Ideal for using in production") checkpointers, plus MongoDB and Azure Cosmos DB ([Checkpointers][lg-cp]). **Inference:** the SQLite checkpointer needs no server, which suits a batch CLI.
- **Storage growth.** "By default, LangGraph checkpoints write the full value of every state channel at each super-step" ([Checkpointers][lg-cp]).
- **Node cache.** A node can have a cache policy whose key is computed from the node's input, with an optional time to live; without one "the cache will never expire" ([Graph API][lg-graph]). Only an in-memory cache backend is documented on that page; a persistent one was not established.
- **Retries.** Default policy: 3 attempts, 0.5 s initial interval, backoff factor 2.0 ([Fault tolerance][lg-ft]).
- **Loop bound.** Default recursion limit is 1000 super-steps since version 1.0.6; exceeding it raises `GraphRecursionError` ([Graph API][lg-graph]).

The standalone "durable execution" page of the LangGraph docs now resolves to the Persistence overview; the content cited above was found on the Checkpointers and Functional API pages.

## Temporal

- **Model.** Workflow code must be deterministic: it "makes the same Workflow API calls in the same sequence, given the same input". On replay the commands it emits "are compared with the existing Event History"; a mismatch fails the execution with a non-determinism error ([Workflow Definition][t-wf]).
- **Where the model call goes.** In an Activity, whose parameters and return value "are recorded in the Event History" and which is not re-executed on replay ([Workflow Definition][t-wf], [Activity Definition][t-act]).
- **At-least-once.** If a worker crashes after an Activity finishes but before reporting it, "the Event History won't reflect the successful completion of the Task, so the Activity will be retried"; "Temporal recommends that Activities be idempotent" ([Activity Definition][t-act]).
- **Code changes.** "The Workflow Definition can change in very limited ways once there is a Workflow Execution depending on it"; calls that produce commands "must not be reordered, added, or removed"; changing Activity types or IDs is unsafe. Patching and Worker Versioning are the sanctioned routes ([Workflow Definition][t-wf]). **Inference:** a changed orchestrator prompt is safe for old runs, because the prompt is Activity input; a changed orchestration code path is not.
- **Limits.** An Event History is limited to 51,200 events or 50 MB, with warnings from 10,240 events or 10 MB; a single request payload is at most 2 MB and a gRPC message 4 MB; Continue-As-New is the remedy for long histories ([Temporal Cloud limits][t-lim], [Events][t-ev]). The 2 MB and 50 MB figures are from the Temporal Cloud page; self-hosted defaults were not checked. **Inference:** large artefacts and tool results must be stored outside the history and referenced by hash.
- **Python.** Workflow files run in a sandbox whose "Restrictions prevent known non-deterministic library calls"; it "is not completely isolated" ([Python sandbox][t-py]).
- **Agent integrations.** In the OpenAI Agents SDK integration, "Agent orchestration—the agent loop, tool selection, and handoffs—runs inside the Workflow" and "model calls are executed as Activities, so they retry durably and are not repeated during Workflow replay"; tools that do I/O must be Activity-backed ([OpenAI Agents SDK integration][t-oai]). No first-party Temporal integration for the Anthropic SDK or the Claude Agent SDK was found; Pydantic AI documents Temporal, DBOS, Prefect, Restate and AWS Lambda backends ([Pydantic AI durable execution][pyd]).
- **Operations.** Temporal needs a Temporal Service (self-hosted or Temporal Cloud) and workers, which is heavier than a library for a batch CLI. DBOS is the library-shaped alternative: it recovers a workflow "from the last completed step" and treats an assigned workflow ID as an idempotency key ([DBOS workflows][dbos]).

## Where replay from a recorded log breaks

| Event | What the frameworks do | Consequence for the catalog |
| --- | --- | --- |
| An input changes mid-run | Temporal: replay diverges and fails with a non-determinism error if the code now emits different commands ([Workflow Definition][t-wf]). LangGraph: nothing detects it; a fork re-executes every node after the chosen checkpoint ([time travel][lg-tt]) | **Inference:** the catalog must hash inputs itself and discard the steps that read the changed input and all steps after them |
| A tool would now return a different result | Replay returns the recorded result and does not call the tool ([Activity Definition][t-act], [Checkpointers][lg-cp]) | **Inference:** replay is faithful to the past, not to the present. Read tools over immutable, hashed inputs are safe; tools that read live systems need their own invalidation rule |
| A new model version | New model means a new ID ([Model IDs and versioning][mid]); thinking blocks do not carry to another model ([Opus 5.5 migration guide][mig55]); caches are model-scoped ([Prompt caching][pc]) | **Inference:** recorded steps still replay; every step not yet recorded is decided by the new model, and a half-finished conversation should be restarted, not continued |
| The orchestrator's code or tool set changes | Temporal requires versioning ([Workflow Definition][t-wf]); changing tools or system prompt mid-conversation invalidates thinking blocks and the prompt cache ([Opus 5.5 migration guide][mig55], [Prompt caching][pc]) | Prompt version and tool-definition hash belong in every step's key |
| A crash between doing and recording | The step runs again ([Activity Definition][t-act], [Functional API][lg-fn], [DBOS workflows][dbos]) | Side-effecting tools need idempotency keys; record "started" before and "completed" after |
| History is compacted | Older turns are replaced by a model-written summary ([Agent loop][sdk-loop]) | Record the summary as a step; keep the raw log separately |

## Cost and rate limits

### Prices (USD per million tokens)

From [Pricing][price] and [Batch processing][batch].

| Model | Input | 5-min cache write | 1-hour cache write | Cache read | Output | Batch input | Batch output |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Claude Opus 5.5 | 4 | 5 | 8 | 0.20 | 20 | 2 | 10 |
| Claude Sonnet 5.5 | 2 | 2.50 | 4 | 0.10 | 10 | 1 | 5 |
| Claude Haiku 5.5 (prompts up to 100,000 tokens) | 0.10 | 0.125 | 0.20 | 0.01 | 0.50 | 0.05 | 0.25 |
| Claude Haiku 5.5 (prompts over 100,000 tokens) | 0.50 | 0.625 | 1 | 0.05 | 2.50 | 0.25 | 1.25 |

- Batch is 50% off input and output, and the cache multipliers "stack with other pricing modifiers, including the Batch API discount" ([Pricing][price]).
- Tool definitions, `tool_use` blocks and `tool_result` blocks are billed as ordinary tokens, plus a tool-use system prompt the API adds ([Pricing][price]).
- Requesting US-only inference multiplies every token price by 1.1 ([Pricing][price]).

### Rate limits

From [Rate limits][rl]. Limits are per organisation and per model, enforced with a token bucket that refills continuously.

| Tier | Monthly spend cap | Requests/min | Input tokens/min | Output tokens/min | Batch requests in queue |
| --- | --- | --- | --- | --- | --- |
| Start | $500 | 1,000 | 2,000,000 | 400,000 | 200,000 |
| Build | $1,000 | 5,000 | 5,000,000 | 1,000,000 | 300,000 |
| Scale | $200,000 | 10,000 | 10,000,000 | 2,000,000 | 500,000 |

The per-minute figures are the same for Claude Opus 5.5, Sonnet 5.5 and Haiku 5.5, each with its own separate limit.

- New organisations "may start in the Evaluation tier, with limits below the standard limits".
- At the tier's spend cap, requests return 429 with no `retry-after` header until the next calendar month, and SDK retries keep failing.
- Tokens read from the prompt cache do not count toward the input-token limit on current models; cache writes and uncached input do.
- Output tokens are counted as generated; `max_tokens` does not count.
- A sharp increase in traffic can trigger 429s from acceleration limits; ramp up gradually.
- "Usage of the Batches API does not affect rate limits in the Messages API" ([Batch processing][batch]).
- A 429 carries `retry-after`; response headers report remaining requests and tokens.

Which tier this project's organisation is on was not established.

### Worked illustration

Arithmetic on assumed sizes, not a measurement. Assume an agent loop of 8 model turns per artefact, a 6,000-token system prompt and tool list shared by all artefacts, a 4,000-token artefact, 1,500 tokens added to the history per turn, and 1,000 output tokens per turn. That is 122,000 input tokens and 8,000 output tokens per artefact, because every turn resends the growing history. The cached rows assume the shared prefix is already in the cache and each turn writes only what the previous turn added.

| Per artefact | Opus 5.5 | Sonnet 5.5 | Haiku 5.5 |
| --- | --- | --- | --- |
| Synchronous, no prompt caching | $0.648 | $0.324 | $0.016 |
| Synchronous, prompt caching with every read hitting | $0.254 | $0.127 | $0.007 |
| Batch, no prompt caching | $0.324 | $0.162 | $0.008 |
| Batch, prompt caching with every read hitting (best case) | $0.127 | $0.064 | $0.003 |

Multiply by 1,000 for a thousand artefacts: $648 uncached on Opus 5.5, which is above the Start tier's $500 monthly cap, against $254 with caching.

Under the same assumptions on the Start tier, the output-token limit binds first with caching on: 400,000 output tokens per minute is 400 turns, or 50 artefacts, per minute. With caching off the input-token limit binds first: about 131 turns, or 16 artefacts, per minute.

What follows from the documented behaviour, whatever the real sizes turn out to be:

- With caching on, output tokens become the larger share of the bill ($0.16 of the $0.254 above), and caching does not reduce them.
- A cache entry is readable only after the first response begins, so starting many artefacts at once makes them all pay for the shared prefix; one request has to go first ([Prompt caching][pc]).
- A turn that starts more than 5 minutes after the previous one for the same artefact misses the default cache, so per-artefact turns should stay close together or use the 1-hour cache ([Prompt caching][pc]).
- A re-run that replays from the catalog makes no API call for recorded steps and so costs nothing for them. A re-run that has to decide again pays in full, because the prompt cache will have expired.

## Not established

- Whether the Messages API supports an idempotency key for safe retries.
- Whether any seed or reproducibility parameter exists; none was found, and the API reference was not searched exhaustively.
- What an Agent SDK session does on resume about a tool call that was in flight when the process died.
- Whether the Agent SDK can send its model calls through the Message Batches API.
- Whether an Agent SDK hook can return a recorded result instead of running a built-in tool; the full hooks reference was only partly read.
- LangGraph's default durability mode, and whether a persistent node-cache backend exists.
- Temporal's self-hosted defaults for payload and history size.
- Whether a first-party Temporal integration for the Anthropic SDK or the Claude Agent SDK exists; none was found.
- Retention of Claude Managed Agents session event history, and Managed Agents pricing; Managed Agents was not examined beyond confirming that a session "maintains conversation history across multiple interactions" ([Managed Agents sessions][cma]).
- This organisation's usage tier, real token counts per artefact, and the cache hit rate a batch would achieve for this workload.

[gloss]: https://platform.claude.com/docs/en/about-claude/glossary
[mig55]: https://platform.claude.com/docs/en/models/opus-5-5/migration-guide
[mid]: https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions
[models]: https://platform.claude.com/docs/en/models/overview
[dep]: https://platform.claude.com/docs/en/about-claude/model-deprecations
[pc]: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
[batch]: https://platform.claude.com/docs/en/build-with-claude/batch-processing
[price]: https://platform.claude.com/docs/en/about-claude/pricing
[rl]: https://platform.claude.com/docs/en/api/rate-limits
[msg]: https://platform.claude.com/docs/en/build-with-claude/working-with-messages
[tool]: https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls
[cma]: https://platform.claude.com/docs/en/managed-agents/sessions
[sdk-sess]: https://code.claude.com/docs/en/agent-sdk/sessions
[sdk-store]: https://code.claude.com/docs/en/agent-sdk/session-storage
[sdk-fcp]: https://code.claude.com/docs/en/agent-sdk/file-checkpointing
[sdk-loop]: https://code.claude.com/docs/en/agent-sdk/agent-loop
[sdk-hooks]: https://code.claude.com/docs/en/agent-sdk/hooks
[sdk-cost]: https://code.claude.com/docs/en/agent-sdk/cost-tracking
[sdk-py]: https://code.claude.com/docs/en/agent-sdk/python
[cc-hooks]: https://code.claude.com/docs/en/hooks
[cc-index]: https://code.claude.com/docs/llms.txt
[lg-cp]: https://docs.langchain.com/oss/python/langgraph/checkpointers
[lg-fn]: https://docs.langchain.com/oss/python/langgraph/functional-api
[lg-tt]: https://docs.langchain.com/oss/python/langgraph/use-time-travel
[lg-graph]: https://docs.langchain.com/oss/python/langgraph/graph-api
[lg-ft]: https://docs.langchain.com/oss/python/langgraph/fault-tolerance
[t-wf]: https://docs.temporal.io/workflow-definition
[t-act]: https://docs.temporal.io/activity-definition
[t-ev]: https://docs.temporal.io/workflow-execution/event
[t-lim]: https://docs.temporal.io/cloud/limits
[t-py]: https://docs.temporal.io/develop/python/python-sdk-sandbox
[t-oai]: https://docs.temporal.io/develop/python/integrations/openai-agents
[t-blog]: https://temporal.io/blog/of-course-you-can-build-dynamic-ai-agents-with-temporal
[dbos]: https://docs.dbos.dev/python/tutorials/workflow-tutorial
[pyd]: https://pydantic.dev/docs/ai/integrations/durable_execution/overview/
