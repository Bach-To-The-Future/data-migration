# Script Migration

Converts the script artefacts of a legacy data platform into equivalent code for a modern data platform. It converts scripts only: it does not move data or schemas.

## Language

### Sources and targets

**Source**:
A legacy platform whose script artefacts are converted, such as SQL Server. A source is a set of artefact types, not a single SQL dialect.
_Avoid_: Source dialect, input language

**Target**:
A platform together with the language of the files produced for it, such as PySpark on Databricks.
_Avoid_: Output dialect, destination

**Client**:
The organisation whose artefacts are converted.
_Avoid_: Customer, end user

**Operator**:
The person who runs the tool on a client's artefacts. In early use the operator is a delivery engineer, not a member of the client's staff.
_Avoid_: User

**Core**:
The part of the tool shared by every source and every target. A new source or target may add operations to the intermediate representation but may never change what an existing one means.

**Engine**:
The whole tool apart from its front ends: the core together with every source and every target.
_Avoid_: Core (when sources and targets are included), backend

**Front end**:
A thin layer through which an operator drives the engine, such as a command line or a graphical interface. It holds no conversion logic.
_Avoid_: UI (when the command line is also meant), app

**Artefact**:
One unit of legacy script handed to the tool: a query batch, stored procedure, view, user-defined function, trigger, SSIS package or SQL Agent job.
_Avoid_: Script (when a specific type is meant), object, file

**Artefact type**:
The kind of an artefact, which decides whether the tool converts it or only recognises it.

**Converted artefact type**:
An artefact type the tool turns into target code.

**Recognised artefact type**:
An artefact type the tool identifies and lists as manual work without producing target code for it.
_Avoid_: Unsupported, skipped

**Table shape**:
The columns and types of a table, used only as input to conversion.
_Avoid_: Schema migration

### Extraction

**Extractor**:
The part of the tool that builds the catalog from a read-only connection to a live server, from folders of exported files, or from both. It never executes client code and never reads a row of client data.

**Catalog**:
The versioned record of everything extracted from a source: table shapes, constraints, references between objects and artefact source code, each fact with where it came from. It is the only thing conversion reads.
_Avoid_: Metadata store, repository

**Snapshot**:
The catalog as one extraction left it. A snapshot never changes, and a run uses exactly one.
_Avoid_: Version, extract

**Control database**:
Where the tool keeps everything it persists for one engagement: catalog snapshots, the run catalog, observations and verdicts, and that client's lessons. There is one per engagement, and deleting a client's data means deleting it.
_Avoid_: Metadata database, repository

### Conversion

**Intermediate representation**:
The form of an artefact that the core works on, independent of any source or target.
_Avoid_: AST, syntax tree, model

**Lowering**:
Turning a source artefact into the intermediate representation.
_Avoid_: Parsing (which is only its first step), translation

**Raising**:
Rewriting part of an artefact's intermediate representation into a form closer to its intent, such as a cursor loop into a set-based step. Lowering never does this; raising is optional and recorded in the conversion report.
_Avoid_: Optimisation, refactoring

**Faithful form**:
The conversion of an artefact with no raising applied: every construct converted as written. It is always produced.
_Avoid_: Literal translation, baseline

**Raised form**:
The conversion of an artefact after raising. It is delivered only if it passes every check the faithful form passes.
_Avoid_: Optimised version, idiomatic version

**Generation**:
Turning the intermediate representation into target code.
_Avoid_: Translation, transpiling

**External reference**:
A reference from an artefact to an object outside the catalog, such as another database, a linked server or a system object.

**Opaque fragment**:
A piece of source kept verbatim, with its position and the reason, because lowering could not express it in the intermediate representation. It is never dropped.
_Avoid_: Flagged fragment (which is target code), unparsed block

**Scope barrier**:
An opaque fragment whose effect on what is in scope cannot be determined, so that everything it might have touched is treated as unknown from that point on.

**Generated description**:
A natural-language account of an artefact rendered by fixed rules from its intermediate representation, and so always faithful to it.
_Avoid_: Summary, documentation

**Intent summary**:
An AI-written account of what an artefact is for, written for readers of the conversion report. It is labelled as such, is never shown to a conversion step, and is never the source of truth.
_Avoid_: Description (which is generated), explanation

**Context builder**:
The part of the core that assembles everything an AI step is shown. An AI step never gathers its own context, and no model call carries anything over from another, except within one repair run.
_Avoid_: Prompt builder, retriever

**Worked example**:
An earlier input and its accepted output, shown to an AI step as a model of what to produce. It comes only from an artefact that reconciled, and is withdrawn if later found wrong.
_Avoid_: Few-shot example, sample

### Conversion output

**Conversion report**:
The per-artefact record stating, for each construct, whether it was converted, converted with a caveat, or left as manual work, and why.

**Flagged fragment**:
A piece of target code the report singles out for review, either because an LLM produced it or because it uses the escape hatch.

**Escape hatch**:
Target code that falls back to embedded SQL because the construct has no reasonable idiomatic equivalent in the target language.

**Session state**:
What converted code carries in place of the legacy database session: temp tables, the open transaction, the last row count, the last identity value and the error being handled. All writes go through it.
_Avoid_: Context (which the context builder assembles), connection

**Single-writer assumption**:
That during a run, converted code is the only writer to the tables it writes. Emulated transactions, assigned identity values and constraint enforcement are correct only while it holds.
_Avoid_: Concurrency caveat, exclusive access

**Support library**:
The versioned package that converted code may import to reproduce source behaviour the target lacks.
_Avoid_: Helper library, runtime, utils

### Verification

**Testing module**:
The part of the core that decides whether a conversion is correct, by checks whose outcome never depends on an LLM.
_Avoid_: Validator, QA

**Reconciliation**:
Running an artefact and its conversion on the same data and comparing the results. When enabled, an artefact counts as converted only if it reconciles.
_Avoid_: Validation (too broad), testing

**Observation**:
What one execution of an artefact produced, in a form shared by every source and target: result sets, output parameters, return value, row counts, any error raised, messages printed, and the state afterwards of every table it wrote, directly or through triggers and foreign-key actions.
_Avoid_: Result, output

**Checkpoint**:
The part of an observation taken after one statement: the variables, temp tables and rows written at that point.
_Avoid_: Trace, snapshot

**Verdict**:
The testing module's judgement of one artefact: a result for each layer of checks and one overall outcome, with the evidence behind it. No LLM takes part in it and nobody can change it.
_Avoid_: Test result, status (which is what the conversion report shows a reader)

**Hold-out seeds**:
Seeds for generating test data that are kept from every step able to change the conversion, and used once, for the final verdict.
_Avoid_: Test set, validation set

**Not reconciled**:
The report status of an artefact converted while reconciliation was disabled.

**Sandbox**:
A disposable instance of a source and a target that the tool controls fully. Reconciliation runs on it with synthetic data, and procedure result shapes are described on it.

**Synthetic data**:
Rows generated from the catalog rather than copied from a client, deliberately including edge cases.

**Corpus**:
A set of artefacts used to judge the tool. The sample corpus is written for this project, the public corpus comes from published sample databases, and the real corpus comes from a client and is kept outside this repository.

### Autonomy

**Autonomy level**:
How much a component decides for itself: deterministic code, LLM step, or agent.

**LLM step**:
One model call with a fixed prompt at a fixed point in a pipeline written in ordinary code. It stays an LLM step when that code makes several calls for candidates, or calls again after a failed check: the code, not the model, decides what happens next.
_Avoid_: Agent (unless the model chooses its own next step)

**Candidate**:
One of several outputs an LLM step produces for the same input. Those that pass every check are kept, and a fixed rule picks one.
_Avoid_: Sample, attempt

**Agent**:
A model in a loop that chooses its own next step and tools.
_Avoid_: Using "agent" for any module or any component that calls an LLM

**Orchestrator**:
The component that decides which step runs next for each artefact in a run.

**Run catalog**:
The record of every step taken during a run, from which a re-run replays earlier steps instead of deciding them again.
_Avoid_: Log, checkpoint store
