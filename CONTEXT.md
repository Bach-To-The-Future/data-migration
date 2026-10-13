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
The part of the tool shared by every source and every target, which no single source or target may require changing.

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
The part of the tool that builds the catalog, either over a read-only connection to a live SQL Server (primary) or from a folder of exported script files (fallback).

**Catalog**:
The versioned record of everything extracted from a source: table shapes, constraints, dependencies and artefact source code. It is the only thing conversion reads.
_Avoid_: Metadata store, repository

### Conversion output

**Conversion report**:
The per-artefact record stating, for each construct, whether it was converted, converted with a caveat, or left as manual work, and why.

**Flagged fragment**:
A piece of target code the report singles out for review, either because an LLM produced it or because it uses the escape hatch.

**Escape hatch**:
Target code that falls back to embedded SQL because the construct has no reasonable idiomatic equivalent in the target language.

**Support library**:
The versioned package that converted code may import to reproduce source behaviour the target lacks.
_Avoid_: Helper library, runtime, utils

### Verification

**Reconciliation**:
Running an artefact and its conversion on the same data and comparing the results. When enabled, an artefact counts as converted only if it reconciles.
_Avoid_: Validation (too broad), testing

**Not reconciled**:
The report status of an artefact converted while reconciliation was disabled.

**Sandbox**:
A disposable instance of a source and a target, filled with synthetic data, on which reconciliation runs.

**Synthetic data**:
Rows generated from the catalog rather than copied from a client, deliberately including edge cases.

**Corpus**:
A set of artefacts used to judge the tool. The sample corpus is written for this project, the public corpus comes from published sample databases, and the real corpus comes from a client and is kept outside this repository.

### Autonomy

**Autonomy level**:
How much a component decides for itself: deterministic code, LLM step, or agent.

**LLM step**:
One model call with a fixed prompt at a fixed point in a pipeline written in ordinary code.

**Agent**:
A model in a loop that chooses its own next step and tools.
_Avoid_: Using "agent" for any module or any component that calls an LLM

**Orchestrator**:
The component that decides which step runs next for each artefact in a run.

**Run catalog**:
The record of every step taken during a run, from which a re-run replays earlier steps instead of deciding them again.
_Avoid_: Log, checkpoint store
