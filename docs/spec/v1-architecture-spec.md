# Script migration tool: v1 architecture spec

## How to read this spec

**What this is.** The architecture of a tool that converts a legacy data platform's script artefacts into code for a modern platform. v1 converts SQL Server artefacts into idiomatic PySpark for Databricks. The spec is ready to hand to a build effort. It was compiled on 18 October 2026 from the decisions on [the wayfinding map][map], and it decides nothing new: every decision here was made in a ticket, and each section links to the tickets and ADRs that made it.

**Where something didn't fit.** Contradictions and gaps found while compiling are listed under [Further Notes](#further-notes), with links to the tickets involved. None is settled silently here: where two decisions disagree, the spec states the later one and flags the disagreement there. An independent review of this spec, and the owner's decisions on it, are recorded in [Independent review of the assembled spec][t28] and applied throughout.

**Conventions.**

- **Terms** are defined in the glossary, [`CONTEXT.md`](../../CONTEXT.md), and used with those meanings throughout. A word in bold at first use is a glossary term or a name this spec gives a decision.
- **"Decided in"** at the head of a section names the tickets the section compiles. Decisions recorded as ADRs name the ADR beside them.
- **Annexes** hold material too long for the body:
  - [Annex A: the operation vocabulary](../spec-drafts/operation-vocabulary.md), generated from [its JSON source][vocab-json];
  - [Annex B: the source and target contract, with the Oracle and Snowflake paper designs](../spec-drafts/source-target-contract.md);
  - [Annex C: silent differences, the SQL Server half](annex-c-differences-sql-server.md);
  - [Annex D: silent differences, the PySpark half](annex-d-differences-pyspark.md);
  - [Annex E: what a 2022 sandbox does not reproduce from SQL Server 2008 to 2016](../research/sql-server-version-differences.md);
  - [Annex F: the extractor's grant, revoke and check scripts](annex-f-extractor-scripts.md);
  - [Annex G: silent differences that agree, what was not established, and what was tested](annex-g-agreements-and-tested-differences.md).
- **Research notes** in [`docs/research/`](../research/) hold the evidence behind several decisions. They are the record of what was found, not part of the contract.
- **Nothing here has been built,** apart from the sandbox, the corpus and the throwaway prototypes named under [Prior art](#prior-art-in-this-repository).

## Problem Statement

An outsourcing company migrates clients off legacy data platforms. The data itself and the table shapes move with existing tools. The **script artefacts** do not: stored procedures, views, functions, triggers and query batches hold the business logic, and today they are rewritten by hand.

Rewriting by hand is slow and expensive, and it fails in ways nobody sees:

- **Silent differences.** The same expression gives a different answer on the new platform, with no error: string comparison that ignores case and trailing spaces in SQL Server and not in Spark, integer division, `NULL` in concatenation, date arithmetic that counts boundaries, rounding of `datetime`. A conversion can look right and corrupt results.
- **The hard artefacts.** Long stored procedures with cursors, transactions, temp tables, `TRY`/`CATCH` and triggers are where hand conversion goes wrong, and where existing converters stop or hand the work to a model without checking it.
- **No evidence.** The client is asked to trust that the new code behaves like the old. Nothing shows which parts were checked, how, and what is left for a person.
- **Repeat work.** A fix made by hand to one converted file is lost when the next batch is converted, and the same mistake recurs across artefacts.

Clients differ in where their code may be sent. Some forbid third-party AI services but allow their own cloud, and some require that the model provider keep nothing.

## Solution

A tool that converts each artefact through a shared core and proves the result by running both versions on the same synthetic data.

- **One core, many sources and targets.** Each source lowers its artefacts into an **intermediate representation** the core owns, in which every operation has one explicit meaning. Each target generates code from it. v1 builds SQL Server as the source and PySpark on Databricks as the target. Oracle PL/SQL and Snowflake SQL are designed on paper to show the core holds for them.
- **Rules first, models for the residue.** Faithful conversion is deterministic. AI takes part in five bounded places, each as an LLM step that code calls, checks and retries. Repair is the only agent. Every model-produced fragment is flagged.
- **Converted code that behaves like the original.** Converted procedures carry **session state** in place of the legacy database session. A tested **support library** reproduces what Spark lacks: transactions, triggers, constraints, identity values, error semantics and every catalogued silent difference.
- **Evidence for every artefact.** A **testing module** runs the original on SQL Server and the conversion on Spark in a local sandbox, compares them in layers, and is built to catch wrong logic that happens to give the right answer. An independent **final validator**, sharing no code with it, checks again. An artefact counts as **converted** only when both agree.
- **A report three readers can use.** A one-page sign-off document for the client and a complete engineering report, both rendered from one deterministic data file.
- **Learning that reaches every artefact.** Fixes, from repair or from a reviewer, become **lessons** applied by exact structural match and confirmed before use. Converted files are never hand-edited and are always regenerated.
- **The client's constraints are kept.** The extractor never runs client code or reads a row of client data. Synthetic data only, never production data. Zero retention can be promised and checked at every model location. Every tool the product uses is free, and open source wherever possible.

Whether the orchestrator, the component that decides what happens next in a run, should be an agent or a plain pipeline is not decided here. Both plug into one interface, and the build's first experiment compares them.

## User Stories

The actors are the **operator** (in v1, a delivery engineer running the tool on an engagement), the **client** (non-technical), the client's **business analyst**, the client's **engineer**, the client's **DBA**, a **reviewer** (a delivery engineer who reviews conversions), a **maintainer** of the tool, a **release reviewer**, and a **builder** implementing this spec.

**Extraction and the catalog**

1. As an operator, I want to extract from a live SQL Server through a read-only login, so that the client can see the tool never changes their system.
2. As a client's DBA, I want a script that grants exactly the permissions the extractor needs and a way to check them, so that I can approve the login without trusting a description.
3. As a client, I want the extractor never to read a row of my data or execute my code, so that granting access carries no data risk.
4. As an operator, I want to extract from folders of exported scripts when no server is available, so that an engagement can start before access is granted.
5. As an operator, I want every fact the tool assumed rather than read to be flagged, so that I know which results rest on settings I supplied.
6. As an operator, I want the extractor to check its own permissions and fail loudly, so that a missing grant never turns into silently empty results.
7. As an operator, I want each extraction to be an immutable snapshot, so that a run is tied to exactly what was extracted.
8. As an operator, I want re-extracting an unchanged server to invalidate nothing, so that re-runs stay cheap.
9. As a client, I want likely secrets in my source text masked before anything reaches a model, and never written into generated code, so that credentials don't leak.
10. As an operator, I want SSIS packages and SQL Agent jobs inventoried and listed as manual work, so that the client sees the whole estate, including what v1 doesn't convert.

**Conversion**

11. As an operator, I want every artefact type that v1 converts (procedures, views, functions, triggers, batches) converted, so that the bulk of the estate moves.
12. As an operator, I want artefacts of any length accepted, so that the longest legacy procedure is not left behind.
13. As a client's engineer, I want converted code to be idiomatic PySpark DataFrame code, so that my team can read and maintain it.
14. As a client's engineer, I want a faithful form of every artefact, and a raised, more idiomatic form only where it passed every check, so that idiom never costs correctness.
15. As a client's engineer, I want each piece of generated code to point back to the source it came from, so that I can review it beside the original.
16. As a client's engineer, I want source comments carried into the generated code, so that the original authors' notes are not lost.
17. As a client's engineer, I want converted procedures to behave like the originals on errors, transactions, row counts and identity values, so that callers keep working.
18. As a client's engineer, I want every silent difference reproduced exactly, or reported where it can't be, so that results don't drift unnoticed.
19. As a client's engineer, I want triggers, constraints and foreign-key actions reproduced by the code that writes, so that legacy logic relying on them still works.
20. As an operator, I want dynamic SQL, unsupported features and unsupported types reported as manual work with a reason, so that nothing is dropped silently.
21. As a client's engineer, I want every model-produced fragment and every use of the `spark.sql` escape hatch flagged, so that I know where to look hardest.
22. As a client, I want table and file names kept as in the source, with every adjustment listed, so that I recognise my own objects.

**Verification**

23. As a client, I want each converted artefact run beside the original on the same synthetic data and compared, so that "converted" is evidence, not a claim.
24. As a client, I want the tests built to catch wrong logic that gives the right answer by chance, so that a pass means something.
25. As a client, I want a second, independent check before anything counts as converted, so that a mistake in the testing module cannot pass a wrong conversion.
26. As a client, I want nobody, person or model, able to change a verdict, so that the report is a record of what the tests found.
27. As an operator, I want to record an acceptance of an artefact that isn't converted, with my name and reason, shown beside its verdict, so that a deliberate decision is visible and honest.
28. As a client, I want test data generated from my schema and my code, never copied from my data, so that no client data enters the sandbox.
29. As a client's analyst, I want to supply business rules the test data should respect, so that tests exercise realistic cases.
30. As a client, I want issues found in my existing logic reported separately from conversion failures, so that I learn what was already wrong.
31. As an operator, I want artefacts the sandbox can't run reported as "not reconcilable locally", with what would make them reconcilable, so that I can widen the extraction or provide a remote tier.
32. As an operator, I want a preview run with reconciliation off, clearly labelled, that converts nothing, so that I can look at output early without anyone mistaking it for converted.
33. As an operator, I want old databases (compatibility levels 80 and 90) reconciled only on a guarded test server that holds nothing of the client's, so that verification never touches production.

**AI use and cost**

34. As a client, I want to choose where the model runs (Anthropic's API, Amazon Bedrock or Google Vertex), so that my code goes only where my policies allow.
35. As a client, I want zero retention by the model provider promised, and checked or attested before any call, with any exception stated, so that I know exactly what is kept.
36. As an operator, I want a cost ceiling per run, with warnings and a projection after the pilot, so that spend never surprises the client.
37. As an operator, I want a run's deterministic work to carry on when the model budget runs out, with every skipped AI step listed, so that budget limits degrade visibly, not silently.
38. As an operator, I want interrupted runs to resume by replaying recorded steps, and re-runs to reuse cached results, so that crashes and re-runs cost little.

**Learning**

39. As a reviewer, I want my fix to a wrong conversion to become a lesson the tool applies wherever the same pattern occurs, so that I fix it once.
40. As a reviewer, I want a lesson confirmed on its own artefacts and against the known-good suite before it applies anywhere else, so that a fix can't break good conversions.
41. As a reviewer, I want to supply target code for one stuck region of one artefact, tested and flagged, so that the team never hand-edits delivered files.
42. As an operator, I want every run to end with all artefacts verified under one frozen lesson set, so that the delivered project is consistent.
43. As an operator, I want to see what changed since the last run and why, so that I can explain differences to the client.
44. As a maintainer, I want lessons learnt on one client never to reach another client except as abstract patterns through a release, so that client code never crosses engagements.

**The report**

45. As a client, I want a one-page sign-off document saying what is converted, what is still being worked on, what needs manual work, and what you need from me, so that I can approve go-live.
46. As a business analyst, I want each artefact described in plain language, with any AI-written description checked against what the artefact actually does, so that I can follow without reading code.
47. As a client's engineer, I want a complete engineering report, worst problems first, with all the evidence, so that I can act on what's wrong.
48. As a client, I want the report in HTML and PDF that can never disagree, so that the signed PDF and the online version are the same record.
49. As a client, I want every degradation, assumption and caveat visible, so that nothing is hidden from me.
50. As a client, I want the conditions the converted code relies on stated once and clearly, starting with the single-writer assumption, and enforced where the tool can, so that my operations team can keep them.
51. As a client's engineer, I want the deployment steps listed, including initialising the identity and row-version counters, so that go-live is correct.

**Delivery and maintenance**

52. As a client, I want the support library's source delivered with my project under a licence that lets me run and maintain it without the vendor, so that I'm not locked in.
53. As a client's engineer, I want projects converted months apart to run side by side in one Databricks environment, so that I don't need an environment per delivery.
54. As a client's engineer, I want to be told when a helper my deployed project uses is found defective, so that I can redeploy.
55. As a maintainer, I want every helper tested against SQL Server across collations, compatibility levels and versions, so that the library is trustworthy.
56. As a maintainer, I want prompts, models and thresholds changed only in releases that pass every gate, so that quality never regresses quietly.

**Orchestration and the build**

57. As a builder, I want both orchestrators to drive the same components through one interface, so that the benchmark compares orchestration alone.
58. As a builder, I want the benchmark's method fixed in advance, so that the default orchestrator is chosen by evidence.
59. As a release reviewer, I want headline numbers measured on a release hold-out never used for development, so that the numbers aren't fitted.
60. As a maintainer, I want a second source or target to need only additions to the core, so that the product can grow without rewriting it.
61. As an operator, I want the engine reachable through a library interface with structured results, so that a graphical front end and client self-service can be added later.

## Implementation Decisions

### 1. Scope and what is delivered

Decided in: [the map's Notes][map], [Does Databricks' own agentic code converter change what v1 should build?][t18], [What representation does the core work on?][t11].

- **Built in v1:** SQL Server as the source, PySpark on Databricks as the target. Every source works with every target through the shared core, and nothing is written per pair of source and target.
- **Converted artefact types:** query and DML batches, stored procedures (control flow, temp tables, cursors, transactions), views, user-defined functions and triggers. **Stored procedures are the lead artefact type**: examples and acceptance criteria are built around them.
- **Recognised artefact types,** identified and reported as manual work with no target code: SSIS packages and SQL Agent jobs. **Dynamic SQL** is a construct inside artefacts and is recognised, not converted. Data-definition statements are read only for table shapes.
- **No size limit.** An artefact of any length is accepted. A long artefact is divided along parse-tree boundaries (statements or nested blocks), never by line count, and each part carries the shared context it needs: declared variables, temp tables, cursors, parameters.
- **Output.** Idiomatic PySpark DataFrame API code in `.py` files. Every write goes through the support library's session state over the Delta Python API. ANSI mode is pinned on in every file. Python-level emulation by tested helpers is preferred over SQL only Databricks can run, so that converted code runs in the sandbox.
- **The escape hatch.** `spark.sql` is used only where neither the DataFrame API nor a helper can express something, or where a client selects Databricks' own transactions. Every use is flagged in the report.
- **Ownership of output.** Converted files are never hand-edited and are always regenerated. Failures are reported in detail with their reason, and a reviewer's fix goes back into the tool as a lesson ([section 13](#13-lessons)).
- **Five selling points**, the first two leading: evidence; the hard artefacts; silent differences handled; repeatable output; target-neutral. [Acceptance Criteria](#acceptance-criteria) makes each one checkable.
- **No dependency on Databricks' conversion tooling** (the agentic code converter, Lakebridge, Genie Code): none of it is called, bundled, copied from or benchmarked against. Databricks as the target platform is unaffected.

### 2. Architecture: engine, components and autonomy levels

Decided in: [What representation does the core work on?][t11], [Which autonomy level does each component get?][t14], [Does Databricks' own agentic code converter change what v1 should build?][t18]. ADRs: [0001][adr1], [0008][adr8].

**The engine and its front ends.** The **engine** is the core together with every source and every target. **Front ends** are thin layers over it, and the command line is the v1 front end. Three rules keep later self-service and a graphical front end possible:

1. Everything the tool does is reachable through the engine's library interface. A front end holds no conversion logic.
2. Progress and results are structured data (the run catalog and the report data), never console output to be parsed.
3. Everything specific to one client (model location, collation defaults, target runtime) is configuration, not code.

In v1 the **operator** is a delivery engineer. Hosting, sign-in, licensing and billing for client self-service are out of scope.

**Components, by side.**

| Side | Component | What it does | Autonomy level |
|---|---|---|---|
| Each source | Extractor | Builds the catalog | Deterministic |
| Each source | Parser | Reads an artefact's text into the source's own tree | Deterministic |
| Each source | Lowering | Turns that tree into intermediate representation, resolving names, types, collations and scopes against the catalog | Deterministic |
| Each source | Recogniser | Identifies and inventories recognised artefact types and constructs | Deterministic |
| Each source | Source notes | The source's quirks and examples, written down for AI steps | Data |
| Each source | Original-code runners | Execute the original in the sandbox, one for each judge | Deterministic |
| Core | Operation vocabulary | Defines the intermediate representation | Data |
| Core | Checker | Verifies the representation after every change | Deterministic |
| Core | Divider | Splits a long artefact along block boundaries, each part with its scope | Deterministic |
| Core | Context builder | Assembles everything any AI step sees | Deterministic |
| Core | Lowering assist | Resolves opaque fragments into representation | LLM step, with candidates |
| Core | Raising, rule cases | Temp table to DataFrame, exit-label `GOTO`, loop-free scalar function, well-nested transaction, fetch loop to for-each-row | Deterministic |
| Core | Raising, hard cases | Cursor to set-based, counted loop to grouping, function with data access to join, upsert to `MERGE`, `DELETE` through a windowed common table expression | LLM step, with candidates |
| Core | Dependency planner | Computes the run plan | Deterministic |
| Core | Testing module: verdicts | Decides whether a conversion is correct | Deterministic; no LLM ever |
| Core | Testing module: proposing extra inputs | Suggests test inputs aimed at unreached code | LLM step |
| Core | Final validator | A second, independent judge | Deterministic |
| Core | Repair | Acts on a failed check | **Agent**, the only one outside the orchestrator |
| Core | Lessons | Drafts, generalises and confirms lessons | Deterministic ([ADR 0010][adr10]) |
| Core | Report builder: data and documents | Produces the report data and renders both documents | Deterministic |
| Core | Report builder: intent summaries | One-line descriptions of what an artefact is for | LLM step |
| Core | Orchestrator, run catalog, cache | Order, checkpoints, reuse | Orchestrator: open, decided by the build benchmark ([section 11](#11-the-orchestrator-interface)) |
| Each target | Idiom mapping and generator | Emit target code | Deterministic |
| Each target | Generation fallback | Target code for a leftover opaque fragment, flagged | LLM step |
| Each target | Target notes | The target's idioms, written down for AI steps | Data |
| Each target | Support library, static check, converted-code runners | Run and check converted code | Deterministic |

**Where AI takes part.** Outside the orchestrator, in five places, each an LLM step that ordinary code calls, checks and retries ([ADR 0008][adr8], amended):

1. lowering assist;
2. the hard cases of raising;
3. the generation fallback;
4. proposing extra test inputs;
5. writing intent summaries.

**Repair is the only agent.** An LLM step stays an LLM step when code calls it several times for candidates or again after a failed check: the code, not the model, decides what happens next. Every AI output is checked by deterministic code before it counts. No model writes report prose, and no model takes part in any verdict.

**Rules that go with the levels.**

1. **Rules first.** An LLM step only attempts constructs no rule covers. If a rule's raised form fails a check, the faithful form is delivered and the rule is reported as faulty. A rule's failure is never handed to a model.
2. **When attempts run out:** lowering assist falls back to the flagged generation fallback, then manual work; raising falls back to the faithful form; repair falls back to manual work, with its evidence in the report.
3. **Limits.** Attempt limits per step and a budget per artefact are fixed and versioned by the tool. Each escalation to a higher model tier counts as an attempt.
4. **Levels can move.** A hard raising case an LLM step gets right every time across the corpus becomes a rule. Repair moves down to an LLM step if the build benchmark shows a single fixed-prompt call fixes as many failures.
5. **Two placements are deliberate.** Lowering assist sits in the core, fed by source notes, because returning representation needs only source knowledge. The generation fallback is the one place pairwise knowledge is allowed, and its output is always flagged. Scopes are worked out by the source during lowering, because scoping rules differ by source; the core's checker verifies them.

### 3. Extraction and the catalog

Decided in: [What does the catalog hold and how does conversion read it?][t8], [What SQL Server permissions does the extractor need?][t25], [Which SQL Server versions does v1 support?][t26], [How does the final independent validator work?][t32]. ADR: [0007][adr7]. Research: [extractor permissions][r-perm].

**Inputs and provenance.**

- An extraction takes at most one live server connection and any number of folders of files. "Live" and "folder" describe which inputs are present; they are not separate modes. Query batches and SSIS packages are files even when a server is available. A live server is the primary path and will normally be provided.
- Every fact records its provenance: read from the server, parsed from a named file, or assumed from a setting.
- One catalog covers one engagement: one server and the databases chosen for it. Anything outside that set is an **external reference**. Adding a database later means a new extraction, not a second catalog.

**The extractor's promises,** stated so a client can be shown them:

1. Its connection is read-only.
2. It never executes client code.
3. It never reads a row from a client table. Row counts come from SQL Server's own metadata, without scanning.
4. An encrypted routine is recorded as present but not extractable, and is manual work.
5. Its login never holds `EXECUTE`.
6. It checks its own permissions before reading and fails loudly if any are missing, because a missing permission makes SQL Server return empty results silently.

**Permissions** (tested on SQL Server 2017 CU31, 2019 CU32, 2022 CU27 and 2025 CU9):

- a login, with a user in each database being extracted;
- `VIEW DEFINITION` on each of those databases;
- `SELECT` on `sys.sql_expression_dependencies` in each, for the dependency cross-check only (optional);
- a user in `msdb` with `SELECT` on `sysjobs`, `sysjobsteps`, `sysschedules`, `sysjobschedules` and `syscategories`.

Nothing at server level and no fixed role: the `msdb` role `SQLAgentReaderRole` can create jobs. On SQL Server 2000, which has no `VIEW DEFINITION`, the login gets database access only and its self-check verifies it holds no `SELECT` on user tables and no `EXECUTE`. Where an old server lets the login read rows anyway, the extractor still never reads a user table, and the report says the promise rests on the tool's behaviour there. The grant, revoke and check scripts are in [Annex F](annex-f-extractor-scripts.md).

**Contents.**

| Level | Held in the catalog |
|---|---|
| Server | Version and edition, build, collation, time zone, nested-triggers setting |
| Database | Collation, compatibility level, `RECURSIVE_TRIGGERS`, the database's default `ANSI` settings |
| Table | Columns with declared type, nullability, collation, default, identity seed and increment, computed-column expression; row count from metadata |
| Constraints | Not-null, check with expression, primary key, unique, foreign key with its actions, unique indexes |
| Routine or view | Source text, parameters with type, default and direction, result shape (or "could not be described" with the reason), and the `ANSI_NULLS` and `QUOTED_IDENTIFIER` settings stored with it |
| Trigger | Table, actions, `AFTER` or `INSTEAD OF`, firing order, enabled or not |
| Other objects | Synonyms, user-defined types and table types, sequences |
| File-based artefacts | Query batches; SSIS packages |
| From `msdb` | SQL Agent jobs |
| Across objects | References between objects, external references |
| Raw capture | The rows read from every metadata view, each view captured wholesale (every column); folder DDL files kept verbatim; on SQL Server 2000, the system tables |
| Tool-recorded | Name adjustments, provenance of every fact |

- **No client data:** nothing derived from table rows, so no statistics and no sample values.
- **No security metadata:** no users, roles or permission assignments, although `VIEW DEFINITION` exposes them.
- **"Wholesale"** means every column of each view the extractor already reads, never additional views such as security catalogs or statistics histograms. The final validator rebuilds the original's database from the raw capture with its own code, so an extraction mistake cannot fool both judges (see [The final validator](#the-final-validator)).
- **Procedure result shapes** are not read by the extractor: SQL Server describes a procedure's result set only to a login holding `EXECUTE`. Each procedure is recreated in the sandbox with the client's settings (compatibility level, `ANSI_NULLS`, `QUOTED_IDENTIFIER`) and described there. A client who wants shapes without the sandbox has their administrator run an export script, read as a folder input. A shape described under assumed settings carries "assumed" provenance. On the corpus, SQL Server could describe 41 of 69 procedures; the other 28 use temp tables or dynamic SQL. For a database at compatibility level 80 or 90, which no sandbox accepts, shapes are described on the guarded remote tier if one exists, else taken from the administrator's export, else recorded as "could not be described"; the static check then skips the shape comparison, as it does for the 28.
- **Time zone.** The extractor calls `CURRENT_TIMEZONE()` on every server (it works from SQL Server 2019 CU32, tested). The time zone is a required setting only when that call fails, as on 2017 and earlier, and for extractions from folders alone.
- **Recognised artefacts.** For an SSIS package or SQL Agent job, the catalog holds an inventory of its contents (tasks or steps, connections, schedules, embedded SQL text) and its references to catalog objects, which show which procedures are entry points. Packages saved with "encrypt all" cannot be inventoried without the key.

**References.** The source's parser works out references from the text, and those are the source of truth. Where a server is present, SQL Server's own dependency metadata is extracted as a cross-check, and disagreements are reported. On SQL Server 2005 the cross-check uses `sys.sql_dependencies`, or is skipped and reported.

**Secrets.** The extractor marks likely secrets by fixed patterns: linked-server logins, connection strings in SSIS packages, credentials in SQL Agent job steps, and connection details in raw rows (for example `provider_string` in `sys.servers`). A marked span stays in the client's own catalog, is masked whenever text is sent to a model or shown in a report, and is never written into generated code: it is replaced by a named setting and listed in the report. Pattern matching will miss some, and the report says so.

**Extraction from folders alone.**

- Table shapes are built by replaying data-definition statements in a fixed order: `CREATE` before `ALTER`, file-name order within each. Two `CREATE` statements for the same object is an extraction error that names both files.
- Required settings: the database default collation and the time zone. Everything else a server would have supplied takes SQL Server's documented default, recorded as assumed.
- Lost without a server: server and database settings, per-routine settings unless the script sets them, row counts, trigger firing order, and confirmation that an object exists.
- The report flags every column whose collation was assumed and gives a completeness summary: how many facts were read, parsed and assumed.

**Versions.**

- Each extraction is an immutable **snapshot** with its own identifier. A run uses exactly one, and the report names it.
- Each object in a snapshot has a content hash, so re-extracting an unchanged server gives identical hashes.
- Each conversion records exactly which catalog facts it read, and its cache key includes a hash of those facts, not the snapshot identifier. A client changing one table invalidates only the artefacts that read it.
- **Name adjustments outlive snapshots.** Where Unity Catalog cannot take a source name, each disallowed character becomes `_`, and names that then collide (including names differing only by case) keep the first in sorted order and give the others a numeric suffix. The same rule, extended to Python identifiers and case-insensitive file systems, applies to file and module names. Adjustments are recorded once in the control database and extended by later extractions, so adding a table never renames an existing one.

**How conversion reads the catalog.** Through one query interface in the core, never by reading storage directly. It answers questions such as "the shape of this table", "what calls this procedure" and "the triggers on this table", and records every read. The catalog stores names as declared; each source supplies its name resolver (default schemas, case-insensitive identifiers, synonyms).

**The catalog's shape holds other sources.** A neutral skeleton every source fills (objects with identities, columns with declared types, constraints, references, source text, provenance), plus a separate extension area per object, named for the source. Core components read only the skeleton; a source's own lowering reads its extensions. An object may contain members, which is what an Oracle package needs.

### 4. The intermediate representation

Decided in: [What representation does the core work on?][t11], [Draft the initial operation vocabulary][t27]. ADR: [0001][adr1]. Annex: [A, the operation vocabulary](../spec-drafts/operation-vocabulary.md).

The core works on an intermediate representation it defines and owns: typed, with every operation carrying one explicit meaning whatever source it came from. Sources lower into it and targets generate from it. A parser's tree is not the shared form, because no one parser reads every source and the usable trees are unlike each other. sqlglot and ScriptDom are tools inside a source (and sqlglot inside a future Snowflake target), never the contract between sources and targets.

1. **Explicit meaning.** Lowering resolves the source's behaviour using catalog types and collations. T-SQL `a / b` on two integers is recorded as integer division; `=` on a case-insensitive column as "equal, ignoring case and trailing spaces"; `money` as "decimal(19,4), multiplication and division truncate", with the original type name kept for the report. Targets never need to know the source.
2. **Add, never change.** A new source or target may add operations, types and node kinds and may never change what an existing one means. A target either handles an operation or declares it unsupported, which the report shows as manual work.
3. **Queries are a logical plan in the core's own vocabulary,** modelled on Spark's logical-plan concepts (read, filter, join, aggregate, project, window, sort, limit…) but never using Spark's plan objects. They keep the source's names (aliases, common table expression names) and its order of steps, so generated code stays recognisable beside the original. Query hints and options are kept as annotations and reported as "not applicable on the target", never dropped.
4. **Lowering is faithful; raising is separate** ([section 5](#5-lowering-raising-and-generation)).
5. **Nothing is dropped silently.**
   - Source that lowering cannot express is kept verbatim as an **opaque fragment**, with its position and the reason. The artefact is still converted; the fragment goes to lowering assist and is flagged.
   - An opaque fragment records what it reads and writes in scope where that can be detected. Where it cannot, it is a **scope barrier**: everything it might touch is unknown afterwards, and the report says so.
   - An artefact that fails to parse at all is divided at boundaries a lexer can still find (`GO` separators, statement terminators), and each part becomes an opaque fragment.
6. **Dynamic SQL has its own node kind,** distinct from an opaque fragment. The expressions building the string are lowered so the report can show what is assembled; the statement is always a scope barrier and is reported as manual work, while the rest of the artefact is converted. Schema changes inside a routine (10 corpus artefacts) and permission changes have node kinds of their own and are reported as manual work.
7. **Who produced each node** is recorded: deterministic code, or an LLM step with its prompt, model and tier. Flagged fragments in the report are derived from that record.
8. **A deterministic checker** runs after every step that creates or changes the representation, whoever made the change. It verifies that every name resolves to the catalog or to something in scope, that types agree, that every node has a source position, and that every opaque fragment is accounted for.
9. **One outer record for every artefact type:** identity, artefact type, tier, dependencies, original source, status. Converted types get the procedural-and-query body. Recognised types get the outer record and an inventory, with no body in v1.
10. **Three mandatory properties:** explicit scopes at every point (parameters, variables, temp tables, cursors, open transactions); source positions on every node, with every piece of generated code pointing back to its nodes; and a saved, versioned document per artefact in the run catalog, its format version part of every cache key.
11. **References between artefacts are by catalog identity, never a copy.** External references (other databases, linked servers, system objects) do not fail the checker, appear in the report, and are ignored by run ordering.
12. **Comments** are annotations on the nearest node, emitted beside the code generated from it, unchanged and marked as from the original. A comment whose code was replaced by raising stays in the report with that transform. All source text, comments included, is untrusted data when it reaches an AI step.

**The operation vocabulary** (Annex A) is a first draft: 18 types, 12 node kinds that are not operations, 7 annotations and 177 operations (52 procedural, 36 query, 89 expression). The hand conversion exercised 85. A check (`corpus/tools/vocabulary_check.py`) confirms that every built-in call, `@@` value and type in the corpus has an entry. Its rules:

- every operation has one meaning, whatever source it came from;
- the vocabulary grows by addition only;
- a target handles an operation or declares it unsupported;
- operations are typed, with one operation per meaning where meaning depends on types (`integer_divide`, `decimal_divide`, `money_divide`, `float_divide`);
- every string comparison names its comparison rule, taken from the source's comparison rules at lowering (for SQL Server, its collation precedence);
- the query part is a logical plan in the core's own words.

Nothing lowers into the vocabulary or generates from it yet; expect operations to be split, merged and renamed when lowering is written against it.

### 5. Lowering, raising and generation

Decided in: [What representation does the core work on?][t11], [How is each construct with no PySpark equivalent converted?][t10], [Which conversion steps need judgement? Hand-convert a corpus sample][t12], [Which autonomy level does each component get?][t14].

**Each artefact goes through a fixed sequence of steps:** lower, lowering assist where needed, check, raise, check, generate, the generation fallback where needed, then the testing module's checks. No orchestrator may skip a step ([section 11](#11-the-orchestrator-interface)).

**Lowering** is deterministic and faithful: it never interprets intent. A cursor is lowered as a cursor and `GOTO` as `GOTO`. The heaviest rule-based piece is type inference: nearly every expression needs its T-SQL type worked out from the catalog. In the hand conversion, 68 of 69 faithful steps were rule-shaped, and the two silent mistakes made by hand (the wrong column counted for `AVG`; a lazy DataFrame recomputed after a write) are what rules get right every time.

**The divider** splits a long artefact along block boundaries, each part carrying its scope, so no artefact is rejected for its length.

**Raising** rewrites a faithful construct into a form closer to intent. Each transform is optional and recorded in the report.

| Transform | Level |
|---|---|
| Temp table to a DataFrame variable, only when filled once and then only read, and no table it was built from is written before its last use | Rule |
| Forward `GOTO` to a cleanup or exit label at the end, into structured code | Rule |
| Loop-free scalar function with control flow into a column expression | Rule |
| A well-nested begin and commit pair into an atomic region | Rule |
| Fetch loop to for-each-row | Rule |
| Cursor to set-based steps | LLM step, 3 candidates |
| Counted loop to grouping | LLM step, 3 candidates |
| Scalar function with data access into a join at each call site | LLM step, 3 candidates |
| "Insert, catch duplicate key, update" into `MERGE` | LLM step, 3 candidates |
| `DELETE` through a windowed common table expression | LLM step, 3 candidates |

**The faithful form is always produced.** A raised form is delivered only if it passes every check the faithful form passes; otherwise the faithful form is delivered and, for a cursor, flagged as row-by-row. In a preview run only the faithful form is delivered, and the report notes that a raised form exists but is unverified. A failure in raising costs idiom, never correctness, and never blocks an artefact.

**Lowering assist** handles opaque fragments: an LLM step that returns intermediate representation, which the checker verifies and generation handles like any other node. Only when it runs out of attempts does the **generation fallback** return target code attached to the fragment: it serves one target only and is always flagged.

**Generation** is deterministic: the type and idiom mappings are fixed and versioned. Untranslatable constructs are detected before generation, never trusted to surface from it (sqlglot was seen dropping `BY SOURCE` from a `MERGE` with nothing logged). Every linked expression is emitted as one contiguous, self-contained span of code, so the testing module can evaluate it alone; a layer-1 check verifies this.

**Clusters.** Two artefacts form a cluster when their lowered representations are identical once every identifier is replaced by a placeholder: literals and parameter defaults must match exactly, and comments are ignored. A cluster shares one conversion. The representative is verified by tests only, every instance is tested individually, and any human acceptance is a recorded acceptance ([ADR 0006][adr6]). An instance matched alone by a client lesson leaves the cluster and gets a conversion of its own.

### 6. Converted code

Decided in: [How is each construct with no PySpark equivalent converted?][t10], [Which conversion steps need judgement? Hand-convert a corpus sample][t12], [Which SQL Server versions does v1 support?][t26], [What must a new source or target provide?][t17], [How does the final independent validator work?][t32]. ADRs: [0002][adr2], [0003][adr3], [0004][adr4], [0019][adr19]. Annexes: [C](annex-c-differences-sql-server.md), [D](annex-d-differences-pyspark.md).

**Policies.**

1. **Silent differences are reproduced exactly,** by a built-in Spark function where one does it and by a tested helper otherwise. Spark's behaviour is accepted, with a caveat in the report, only where reproduction is impossible. Never silently, and never trading correctness for speed without saying so. There is no relaxed mode per client.
2. **ANSI mode is on,** pinned in every file.
3. **Python-level emulation is preferred over SQL only Databricks can run,** so converted code stays reconcilable in the sandbox. Where no locally runnable form exists, the artefact is reconciled on an optional remote tier or reported as "Not reconcilable locally" ([ADR 0014][adr14]).
4. **Writes** use the Delta Python API (`DeltaTable.update`, `.delete`, `.merge`), which is idiomatic and not flagged, always through session state's write wrappers. Reads stay plain DataFrame code.
5. **What converted code may assume about target tables:** names identical to the source (`database.schema.table` becomes `catalog.schema.table`, with the fixed adjustment rule); types by the fixed mapping below; nothing else. No collation on target columns, no enforced keys, no triggers, no identity columns. One requirement: each `datetimeoffset` column has a companion column holding its offset. Every assumption and requirement is listed in the report, and the static check builds its empty tables with the same mappings.

**What each artefact type becomes.**

| Artefact type | Converted form |
|---|---|
| Stored procedure | A Python function. Parameters become keyword arguments. It returns one result object: the return code, the output parameters and the result sets as an ordered list of DataFrames |
| View | A function returning a DataFrame |
| Scalar function, no data access | A function building a column expression, so it runs inside Spark |
| Table-valued function | A function returning a DataFrame |
| Trigger | A function taking the inserted and deleted rows as DataFrames |
| Query/DML batch | A script with one function per `GO`-separated batch and a list giving their order |

A scalar function that cannot be a column expression is first raised to one (`IF`/`ELSE` chains become `when`/`otherwise`), then falls back to a Python UDF flagged for performance. A scalar function with data access is rewritten as a join at each call site, or is manual: code inside a Python UDF cannot query Spark.

**Procedural code and twins.** Variables, `IF` and `WHILE` become plain Python only where lowering proves a value cannot be `NULL` and the types match. Otherwise a pure-Python **twin** helper is used, because a comparison with `NULL` is "unknown" in T-SQL and an exception in Python, typed variables cut or convert values on assignment, and functions such as `DATEADD` need T-SQL's behaviour on single values. Every helper used in procedural code or inside a Python UDF needs a twin, tested against SQL Server like the others. Where none exists, the construct is manual.

**Session state.** One support-library object, passed as the first argument to every converted function (a convention of the PySpark target). It carries temp tables, the open transaction, `@@ROWCOUNT`, `@@ERROR`, the last identity value, the error being handled, messages, and session values. Its write wrappers record row counts, register with the open transaction, enforce constraints, fire triggers and record which columns an `UPDATE` named. One uniform way to write keeps the correctness-critical bookkeeping in one tested place.

**Construct mapping.**

| Construct | Conversion | Class |
|---|---|---|
| `INSERT`, `UPDATE`, `DELETE`, `MERGE` | Session-state write wrappers over the Delta Python API | Support library |
| Constraints and foreign-key actions | Enforced by the wrapper from the catalog ([ADR 0003][adr3]) | Support library |
| Transactions and savepoints | A `transaction()` block, compensating restore by default ([ADR 0002][adr2]) | Support library, flagged |
| `TRY`/`CATCH`, `THROW`, `RAISERROR`, `ERROR_*()` | `try`/`except` through a helper with the error mapping | Support library |
| Statement failure without `TRY` | Guarded blocks where `@@ERROR` is read; otherwise stop, reported ([ADR 0004][adr4]) | Support library, or accepted with caveat |
| `@@ROWCOUNT`, `OUTPUT` | Session state; the inserted and deleted row mechanism | Support library |
| Identity, `SCOPE_IDENTITY()`, `@@IDENTITY` | A wrapper-kept counter | Support library, flagged |
| `timestamp` (`rowversion`) | A counter for the whole database, assigned on every insert and update | Support library |
| Triggers | Functions fired by the write wrapper | Support library |
| Temp tables, table variables | A scratch Delta table, or raised to a DataFrame | Support library, or idiomatic when raised |
| Cursor | Raised to set-based, or a streamed driver loop | Idiomatic when raised; otherwise flagged |
| Variables, `IF`, `WHILE`, `BREAK`, `CONTINUE`, `RETURN` | Plain Python where proven safe, otherwise twins | Idiomatic or support library |
| Variable assigned from a query | Two helpers, one per form | Support library |
| `GOTO` | Raised for the exit-label shape, otherwise manual | Idiomatic or manual |
| `PRINT`, `RAISERROR` at severity 10 or below | Messages in session state | Support library |
| Table-valued parameter | A DataFrame argument | Idiomatic |
| Dynamic SQL; text-pointer statements (`READTEXT`, `WRITETEXT`, `UPDATETEXT`, `TEXTPTR`, `TEXTVALID`); `COMPUTE`/`COMPUTE BY` | Not converted | Manual |
| Point-in-time reads | A query over the current and history tables with the period predicates, never Delta time travel | Support library |
| Recursive common table expressions | An iterative helper keeping `MAXRECURSION` (100 by default, error 530 when exceeded) | Support library |
| `FOR JSON` | A helper reproducing its formatting, omitted nulls and escaping | Support library |
| XML `.value()`, `.exist()`, `.nodes()`, `OPENXML` | Helpers for a defined XPath subset; anything outside it is manual | Support library, or manual |
| Full-text search | Declared unsupported by the target | Manual |

**Transactions and savepoints** ([ADR 0002][adr2]).

- Generated code always uses one helper, a `transaction()` block, whose mechanism is a setting.
- **Default: compensating restore.** The helper notes each written table's Delta version at `BEGIN TRAN` and restores those versions on `ROLLBACK`. It runs in the sandbox. Start versions are recorded in the run catalog during testing, and in the support library's state table in production. At the start of every run, session state recovers any unfinished transaction left by a crash before any other work ([ADR 0021][adr21]). The helper fails loudly if a version has been removed by `VACUUM`. Table variables are left out of the restore, as in T-SQL.
- **Alternative: Databricks' own transactions** (SQL only, Runtime 18 or later, Unity Catalog managed tables). The same generated code switches over; reconciliation then needs a remote tier or reports "Not reconcilable locally".
- **When a statement fails inside a transaction block,** the block rolls everything back, with the ADR 0004 caveat in the report. `XACT_ABORT ON` and procedures with guarded statements are exact and need no caveat.
- **This is not isolation:** it rests on the single-writer assumption, enforced among converted jobs by write leases ([ADR 0021][adr21]), and every transaction is flagged.

**Triggers.**

- A trigger becomes a function of (inserted rows, deleted rows). Every converted write to its table goes through the wrapper, which works out those rows and calls it; for `INSTEAD OF`, in place of the write. The wrapper runs a write and its trigger calls inside a `transaction()` block.
- Inserted and deleted rows are worked out from the primary key in the catalog; with no key, from Delta's change data feed if it is on; with neither, the write is manual.
- Nested triggers follow from trigger functions writing through the wrapper, which enforces SQL Server's 32-level limit and raises the same error. Recursive triggers follow the database's `RECURSIVE_TRIGGERS` and the server's nested-triggers option. Several triggers on one table fire in the order the catalog records; where SQL Server leaves it undefined, the report says so.
- The report states that writes from outside the converted code do not fire triggers.

**Temp tables and table variables.** The faithful form is a Delta table in a per-run scratch schema, held in session state under its original name, dropped at the end and registered in the run catalog during testing and in the state table in production, so a crashed run's scratch tables are dropped at the next start. Visibility follows call depth: a temp table created in a called procedure is dropped when it returns, and a callee may shadow a caller's `#name`. Global temp tables (`##name`) are reported, not treated as private. Temp-table columns take the server's default collation unless the code says `COLLATE DATABASE_DEFAULT`.

**Cursors.** The raised form is a set-based step. The faithful form is a driver loop that streams the cursor's rows (`toLocalIterator`), never collecting them all, flagged as row-by-row. It reads a snapshot taken when the cursor opens; `DYNAMIC` and `KEYSET` cursors, and `FAST_FORWARD` loops that write their own source table, are reported with a caveat. `UPDATE … WHERE CURRENT OF` becomes an update by primary key, or is manual without one.

**Errors.**

- `TRY`/`CATCH` becomes `try`/`except` through a helper that re-raises the errors T-SQL's `CATCH` would not have caught, such as a missing object referenced at the same level (finding 7.6 in [Annex G](annex-g-agreements-and-tested-differences.md)). `RAISERROR` at severity 10 or below is a message, never an exception.
- Errors raised by converted `THROW` or `RAISERROR` keep their exact number, message, severity and state.
- Spark and Delta failures are mapped to the core's neutral **error classes** by the target, and error classes to SQL Server numbers, messages and severities by the source ([ADR 0019][adr19]). The mapping records, for each error, whether SQL Server ends the statement or aborts the batch. An unmapped error gets a reserved number, and any source comparison against a number the mapping doesn't cover is flagged.
- **A failed statement that does not stop the procedure** (`XACT_ABORT` off, no `TRY`): in procedures that read `@@ERROR`, every statement sits in a guarded block that records the error and continues or aborts as SQL Server would. Every other procedure stops at the first error, and the report states the difference ([ADR 0004][adr4]).

**Identity values and row versions.** The write wrapper assigns identity values itself from the seed and increment in the catalog, so `SCOPE_IDENTITY()` and `@@IDENTITY` are exact, including their difference when a trigger inserts elsewhere. Its counter is never reset by a rollback, matching SQL Server's gaps. Row versions (`timestamp`) come from one counter for the whole database. Both rest on the single-writer assumption and are flagged. In production, counters live in a state table ([section 7](#7-the-support-library)). Relying on Databricks identity columns instead is a setting; every `SCOPE_IDENTITY()` is then manual and the artefact cannot run in the sandbox.

**Constraints** ([ADR 0003][adr3]). The wrapper checks not-null, check, primary key, unique and foreign key constraints from the catalog before each write and raises the mapped SQL Server error. Foreign-key actions (`CASCADE`, `SET NULL`, `SET DEFAULT`) are reproduced. Every write pays a lookup, which the report states. Enforcement is not atomic with the write: the single-writer assumption again.

**Assigning a variable from a query.**

| Form | No rows | Several rows |
|---|---|---|
| `SELECT @x = col FROM …` | `@x` keeps its old value | `@x` gets the last row processed, which is undefined; flagged unless the catalog shows one row only |
| `SET @x = (SELECT …)` | `@x` becomes `NULL` | Error |

**Values that depend on the moment or the machine.** `GETDATE()` and relatives come from session state in the source server's time zone, evaluated once per statement. `SUSER_SNAME()`, `HOST_NAME()`, `DB_NAME()` and `@@SPID` come from settings, mapped from the Databricks runtime by default. `NEWID()`, `RAND()` and the order of `TOP` without `ORDER BY` cannot be reproduced; Spark's behaviour is accepted with a caveat.

**Collation** follows SQL Server's full precedence, for comparisons and constraint enforcement alike: an explicit `COLLATE` wins, then a column's collation, then the default, covering variables and literals as well as columns. Comparing two different implicit collations raises the collation-conflict error. Comparison helpers that normalise case and trailing spaces apply only where the effective collation is case-insensitive; the performance and data-skipping cost is accepted. `GROUP BY` and `DISTINCT` on such values group on the normalised value and return an original spelling, never a normalised one.

**Silent differences handled.** Every entry in Annexes C and D has a treatment. The main ones:

| Difference | Conversion | Class |
|---|---|---|
| Case-insensitive and trailing-space comparison in filters, joins and `MERGE` keys | Comparison helper | Support library |
| `GROUP BY`/`DISTINCT` on those values | Normalised grouping, original spelling returned | Support library |
| `uniqueidentifier` comparison and ordering | SQL Server's own byte order | Support library |
| Integer division; `AVG` and `SUM` result types | `div` and explicit casts | Idiomatic |
| `+` and `CONCAT` with `NULL`; `LEN`; `ISNULL` result type | Built-ins | Idiomatic |
| `DATEDIFF`, `DATEPART(week/weekday)`, date rounding | Helpers | Support library |
| `SUBSTRING`, `CHARINDEX`, `STUFF` out of range; `LIKE` with `[a-c]` | Helpers | Support library |
| `ISNUMERIC`; `CONVERT` with a style code | Helpers; an unlisted style is manual | Support library |
| `money` arithmetic | A `money()` wrapper whose operators truncate at every step | Support library |
| `TOP n WITH TIES` | A rank window | Idiomatic |
| String ordering under a case-insensitive collation: `ORDER BY`, `MIN`, `MAX`, `<`, `BETWEEN` (2.7) | Helper ordering by SQL Server's collation; a caveat where it cannot match exactly | Support library |
| `REPLACE` and `CHARINDEX` under a case-insensitive collation (2.11) | Helpers comparing by the collation | Support library |
| `ROUND` with a non-zero third argument, which truncates (3.6) | Helper | Support library |
| `float` to string, style 0 (3.10) | Helper reproducing SQL Server's formatting | Support library |
| `TOP` without `ORDER BY`; `NEWID()`; `RAND()` | Spark's behaviour | Accepted with caveat |
| Query hints | Kept as annotations | Reported as not applicable |

**The type mapping, in two halves** ([ADR 0019][adr19]). One mapping, fixed by the tool, never configurable per client, versioned with the support library and part of every cache key.

The SQL Server half (source types to vocabulary types; see Annex A for each type's meaning):

| SQL Server type | Vocabulary type |
|---|---|
| `bit` | `boolean` |
| `tinyint`, `smallint`, `int`, `bigint` | `integer(bits, signed)`; `tinyint` is `integer(8, unsigned)`, 0 to 255 |
| `decimal(p,s)`, `numeric(p,s)` | `decimal(precision, scale)` |
| `money`, `smallmoney` | `money(precision)` |
| `float`, `real` | `float(bits)` |
| `date` | `date` |
| `datetime`, `smalldatetime`, `datetime2` | `local_timestamp(rounding)` |
| `datetimeoffset` | `timestamp_with_offset(digits)` |
| `time` | `time_of_day(digits)` |
| `char`, `nchar`, `varchar`, `nvarchar`, `text`, `ntext`, `sysname` | `string(length, unit, fixed, collation)` |
| `binary`, `varbinary`, `image` | `binary(length, fixed)` |
| `uniqueidentifier` | `guid` |
| `xml` | `xml` |
| `timestamp` (`rowversion`) | `binary(8, fixed)`, assigned by the database-wide counter |
| `sql_variant`, `hierarchyid`, `geography`, `geometry` | `unsupported_type`: anything touching it is manual |

The PySpark half (vocabulary types to Spark types, with the representation rules published in every delivered project):

| Vocabulary type | Spark type | Representation rule, reproduced by the write wrapper or a helper |
|---|---|---|
| `boolean` | `BOOLEAN` | |
| `integer(8, unsigned)` | `SMALLINT` | Range 0–255 enforced |
| `integer(16/32/64)` | `SMALLINT`, `INT`, `BIGINT` | |
| `decimal(p,s)` | `DECIMAL(p,s)` | |
| `money(19)`, `money(10)` | `DECIMAL(19,4)`, `DECIMAL(10,4)` | Truncating arithmetic |
| `float(53)`, `float(24)` | `DOUBLE`, `FLOAT` | |
| `date` | `DATE` | |
| `local_timestamp` | `TIMESTAMP_NTZ` | Rounding to 1/300 s (`datetime`) or to the minute (`smalldatetime`); `datetime2(7)` keeps six fractional digits, and the seventh is compared within a catalogued 100 ns tolerance, reported per artefact |
| `timestamp_with_offset` | `TIMESTAMP` plus a companion offset column | The offset is kept in the companion column, a target-table requirement listed in the report; seven fractional digits as for `local_timestamp` |
| `time_of_day` | `STRING` | Fixed-width, zero-padded `HH:MM:SS.fffffff`, so text order is time order; flagged |
| `string` | `STRING` | `char(n)` padded to `n`; length enforced with SQL Server's truncation error, counted as SQL Server counts (UTF-16 units for `nvarchar`, bytes for `varchar` under UTF-8 collations) |
| `binary` | `BINARY` | |
| `guid` | `STRING` | Compared and ordered through the byte-order helper |
| `xml` | `STRING` | Flagged |
| `binary(8, fixed)` for row versions | `BINARY` | Compared by property, never by raw value |

**The escape hatch.** None of the constructs above needs `spark.sql` by default. It remains for a client choosing Databricks' own transactions, and for any query shape generation finds it cannot express in the DataFrame API. Each use is flagged; one that cannot run in the sandbox goes to a remote tier or is "Not reconcilable locally".

**The delivered project.**

- **Layout:** `converted/<Database>/<schema>/<kind>/<Name>.py`, one artefact per file, names kept as in the source under the adjustment rule. A callee is imported as a module, so a dependency is an import.
- **Carried with it:** the vendored support library ([section 7](#7-the-support-library)); a settings file generated from the catalog snapshot and the engagement; a **manifest of entry points**, one per artefact, saying how to invoke it and read its results; the type mapping's **representation rules**; the definition of the state table; the counter initialisation command; the recovery command.
- **Source comments** appear beside the code generated from them, marked as from the original.

### 7. The support library

Decided in: [How is the support library packaged, versioned and tested?][t22], [What must a new source or target provide?][t17]. ADRs: [0018][adr18], [0019][adr19].

The support library implements, for one target, the vocabulary operations that target's built-ins don't. For PySpark it is one pure-Python wheel, here called `migrt_spark` (the import name says which target it serves). A built-in Spark function is preferred wherever one exists.

**Packaging and licence.**

- One wheel, with no compiled code and no third-party dependencies of its own. PySpark and Delta are declared as compatible ranges and supplied by the runtime, never installed over it.
- The delivered project carries the wheel with its hash and pins it exactly. The client installs it from the project (for example through a Unity Catalog volume or a job environment), with no package index, so air-gapped clients work and the final validator installs exactly what ships.
- **Licence:** the source ships in the delivered project under a source-available licence: perpetual, for the client's internal use and for modification in their own maintenance, with no redistribution and no use to build a competing tool.

**Versions.**

- **One version for three things.** The type mapping (target half) and the error mapping (target half) are data files inside the package, so the library version identifies all three. It is the one version in cache keys, and the generator reads the type mapping from the same package.
- **What the numbers mean:** a patch fixes a bug and changes behaviour only to fix it; a minor release adds helpers; a major release changes signatures. Each tool release pins one library version.
- **Versioned names.** Each delivered project vendors the library under names that carry its full version: the import name (for example `migrt_spark_v1_4_3`), the distribution name (`migrt-spark-v1-4-3`), and Spark function and temp-table names. Projects pinned to different versions can share one Databricks environment, and a project runs only with the version it was verified under.
- **One shared state table.** The state table is shared, not versioned: two projects writing the same tables must take identity values from the same counters and see each other's write leases. Its schema is versioned and only ever extended, and every library version reads and writes it the same way.
- **Supported runtimes.** v1 supports Databricks Runtime 17.3 LTS and 18 LTS, both Spark 4 with ANSI mode on by default. Runtime 16.4 LTS (Spark 3.5) is not supported. The sandbox follows the client's declared runtime, so the product ships one Spark image per supported runtime.

**When the library changes.**

- The library version is part of the verdict key and the validator key, so a new version re-verifies and re-validates every artefact that imports it. Generated code is reused where nothing else changed; AI steps re-run only if the helper API description they are given changed. The report's "changes since the last run" lists every artefact whose verdict changed.
- Deployed code keeps its pinned version, and the tool cannot reach production. Each release ships a **defect advisory list** (helpers fixed, versions affected), and a `check-project` command reads a delivered project's pins and lists the artefacts using an affected helper, without running anything.

**Settings per client.** The delivered project carries a settings file generated from the catalog snapshot and the engagement, read by session state at startup.

- **Semantic settings,** fixed and fingerprinted because they are part of what was verified: the transaction mechanism, the identity mechanism, the default collation, the compatibility level, `DATEFIRST`, `DATEFORMAT` and language, and the time zone. Changing one means regenerating and verifying again. Helpers whose behaviour a compatibility level gates read the level from session state.
- **Runtime identity values,** mapped from the Databricks runtime by default (`SUSER_SNAME()` becomes the current Databricks user), and overridable at deployment.

**Counter state in production.**

- **The state table.** A library-owned Delta table, in a schema the client chooses, holds the identity counters, the row-version counter (each database's `@@DBTS`), **write leases**, transaction start versions and scratch-table registrations. The project includes its definition, and the validator creates it in its fresh sandbox.
- **Write leases** ([ADR 0021][adr21]). Each delivered job takes a lease on every table it writes before its first write. A second converted writer that finds a live lease fails at once with an error naming the table and the holder. This enforces the single-writer assumption among converted jobs; writers outside the converted code are still bound by it, and the sign-off says so.
- **Recovery on start** ([ADR 0021][adr21]). At the start of every run, session state checks for unfinished transactions and scratch tables left by a crash and recovers them before any other work: it restores the recorded versions, drops the scratch tables and releases the leases. The recovery command ships in the project for manual use.
- **The counter initialisation command** must run at go-live and after any bulk load that bypasses the write wrappers; the report lists it as a required deployment step. Identity counters start at the highest existing value, honouring seed and increment. It takes an optional file of per-table starting values, exported by the client's DBA at cutover (`IDENT_CURRENT`, `@@DBTS`), and each counter starts from the larger of that value and the data's highest, because SQL Server's next value can be higher after deletes or rollbacks. Without the file it uses the data alone and warns, and the report lists every counter so initialised. The testing module and the validator run the command after loading test data, and exercise the leases and the recovery, which tests them.

**The API manifest.** A machine-readable manifest of the library's API, versioned with it, gives for each helper its signature and meaning, the silent differences it handles, its twin, and the naive built-in it replaces (for mutation testing). It is the single source for the context builder, mutation testing and the documentation. The final validator never reads it.

**Smaller API decisions.**

- `ss.snapshot(df)` writes to a Delta temp table scoped to the session, used wherever a value is read after a write to one of its sources. Spark's `cache()` is never used for this, because a cached DataFrame can be evicted and recomputed.
- The write wrapper records which columns an `UPDATE` named and exposes them to triggers, for `IF UPDATE(col)`.
- `money()` wraps values so its operators truncate at every step.
- `ss.typed(value, type)` is the one way to bring in a typed value.

**Testing the library** is described under [Testing the tool itself](#testing-the-tool-itself).

### 8. AI steps

Decided in: [What context does each AI step receive?][t21], [How does a run govern its model use?][t30], [Which autonomy level does each component get?][t14], [What does the conversion report look like?][t9]. ADRs: [0008][adr8], [0009][adr9], [0016][adr16].

**Every AI step is shown only what the context builder assembles, and every model call is stateless** ([ADR 0009][adr9]). No conversation history carries between steps, artefacts or candidates. The one exception is a repair run, whose context grows during its work on one failure.

**What each step gets.** Every step also gets its instructions, its output format, and client text marked as data.

| Step | Gets | Withheld beyond the general list |
|---|---|---|
| Lowering assist | The opaque fragment's source and position, the scope at that point, the enclosing block's generated description, the catalog slice, the relevant construct-mapping and silent-difference entries, the whole operation vocabulary, worked examples, applicable lessons | Target conventions and the helper API |
| Raising, hard cases | The faithful-form region with the generated description of it and its surroundings, the original source of that region, the scope, the catalog slice, the transform's name and preconditions, the operations in the region plus those the transform may emit, worked examples of that transform, applicable lessons | The generated code; the intent summary |
| Generation fallback | The leftover fragment (source and representation), the scope, the catalog slice, the helper API, the target conventions, the import allow-list, the relevant silent-difference entries, worked examples, applicable lessons | The operation vocabulary |
| Repair | Everything raising or lowering assist would get for the failing region, plus the evidence and tools in [section 9](#9-repair) | Hold-out seeds not yet disclosed, thresholds, data-generator internals, the business-rules file, domain profiles |
| Proposing extra test inputs | The original source, the catalog slice, coverage gaps of the original with their status, the domain profiles, the business-rules file, the input format | Every product of conversion, including the faithful representation (ADR 0005); intent summaries |
| Intent summaries | The original and its generated description | Everything else |

**Withheld from every step:** other artefacts' full source, test thresholds, data-generator internals, the final validator's evidence, and anything over the step's size budget.

- **The scope at a point:** variables in scope with types and known values; live temp tables and table variables with their shapes; session settings and the transaction state; open cursors; whether a scope barrier has already occurred.
- **Called artefacts** appear as a signature, a result shape and an effect summary (tables read and written, whether it raises, whether it touches session state). A transform that inlines a callee gets the callee's faithful representation as part of the region, never its source text.
- **The catalog slice** comes through the query interface, which records every read. Marked secrets are masked, and facts carry provenance, so a step can be told that a type or collation was assumed.
- **The vocabulary.** Lowering assist gets the whole vocabulary, about 9k tokens, as a fixed prefix that can be cached. Raising and repair get only the operations in the region plus those the transform may emit. Construct-mapping and silent-difference entries are always filtered to the constructs present.
- **Real names, not placeholders.** Conversion steps see real identifiers, because names carry intent; tests guard against names biasing a model. Placeholders are used only for embeddings and example matching.

**Output formats.** Every step answers in a typed structure through tool use, checked against a schema; an answer that fails the schema is a failed attempt.

- **Lowering assist:** a fragment of representation in JSON, with a source-position link on every node.
- **Raising:** a replacement region of representation, plus the transform's preconditions as claims that can be checked (for example "the passes are independent: reads {T}, writes {U}"), verified by code where it can.
- **Generation fallback:** one Python function with a fixed signature (session state and arguments) that imports only from the allow-list.
- **Repair:** a patch to a named region, at a level it may change, with a one-line hypothesis.
- **Proposed test inputs:** documents in the generator's typed input format, each naming the statement or branch of the original it targets.
- **Intent summaries:** one line of text, shown in the sign-off only after a deterministic check ([section 15](#15-the-conversion-report)).

**Size budget.** A token budget per step, versioned.

- **Never trimmed:** the instructions, the output format and the target conventions; the fragment itself, its scope, the catalog facts for objects it references and the silent-difference entries for its constructs; the applicable-lessons slot. If these don't fit, the fragment is split along the parse tree; if one parse-tree node still doesn't fit, the region becomes manual work, with the reason in the report.
- **Trimmed, in order:** worked examples (from 3 down to 0), the generated description of the surroundings, then the vocabulary's descriptions (operation names are kept).

What was trimmed is recorded with the step.

**Worked examples.**

- **Pools.** The global pool holds conversions from the sample and public corpora, made during the build, never including the release hold-out. The engagement pool holds this client's own artefacts and never leaves its control database. With an equal structural match, engagement examples rank first. The nine hand conversions join the global pool only once they reconcile.
- **What qualifies:** a verdict of "Reconciled" with mutation testing run and passed, tracing available, and final validation passed. Not "Reconciled, weakly tested", not an artefact resting on a recorded acceptance, not one flagged "relies on an accepted dependency". Examples from bulk artefacts are usable only from the next run, because the bulk is validated at the end.
- **Selection.** Vector search on placeholder embeddings only proposes; the choice is an exact structural comparison of the constructs present, one example per structural pattern. Zero examples is valid. An example is shown in the step's own output format. Repair gets no worked examples.
- **Retiring an example.** An example is retired when its artefact fails a later verdict or final validation, when a reviewer's fix or a lesson changes its accepted output, or when the rule or transform it illustrates is reported faulty. Every artefact whose recorded example ids include it is then re-checked under the updated test definition, including any new test inputs the lesson or fix adds; with no new inputs, the affected regions are converted again without the example, and any artefact whose output changes is tested again.

**Candidates.** Lowering assist and hard raising produce 3 candidates (a versioned setting), given different context so their errors are independent: candidate 1 the standard context, candidate 2 different worked examples (the next-ranked matches, then the other pool's, then none), candidate 3 no examples. When two candidates would still get the same context, each uses a different instruction phrasing from a fixed, versioned set. The models used reject sampling parameters, so candidates differ only by context variant and tier. A setting, off by default, lets one candidate use a model from a different tier. With zero matching examples, the report notes that the candidates' errors may not be independent. How candidates are checked against each other is under [The inner checks](#the-inner-checks).

**Retries.** A retried step gets its own failed output and the checker's deterministic message, appended after its original context. It never sees other candidates' outputs.

**Client text is data.** Every piece of client text (source, comments, string literals, object names in descriptions) goes in a delimited data block whose boundary marker is derived from the content's hash, so the content cannot contain it. The instructions say nothing inside a block is an instruction. Comments are included and labelled untrusted.

**Recorded per call, and the cache.** For every call the run catalog records the context builder version, the prompt version with its instruction phrasing, the model actually used, the complexity score and tier, the example ids, the recorded catalog reads, what was trimmed, and the hash of the rendered context. The rendered context is stored compressed in the engagement's control database. The **cache key** is the hash of the rendered context, plus the prompt and model versions, the candidate index with its context variant, the tier and score, and the attempt number. The cache lives in the engagement's control database and is never shared across engagements.

**Model tiers and routing.**

- A deterministic **complexity score** per fragment or region, computed from the representation (size, nesting depth, hard constructs present, scope carried, tables and callees touched), with a versioned formula. The artefact's overall score sets a minimum tier.
- **Tiers** are a versioned setting, for example: low, Claude Sonnet 5.5 at medium effort; medium, Claude Opus 5.5 at high effort; high, Claude Opus 5.5 at extra-high effort.
- **Escalation:** a retry after a failed check moves up one tier; when every candidate fails, the whole step moves up. Each escalation counts as an attempt. Repair always runs at the top tier, so it never switches model mid-run.
- **Measured routing:** a cheaper tier is used for a complexity band only if, over three cold runs with at least 20 steps in the band, its pass rate is at least the top tier's, measured outside the release hold-out. **The first release routes everything to the top tier,** except intent summaries, which may use the low tier. The cost: v1 starts at the highest spend.
- **Routing belongs to the step, never to the orchestrator.**

**Model locations and retention** ([ADR 0016][adr16]). The tool always uses Claude; there is no mode without a model. Three locations: Anthropic's API directly, Amazon Bedrock, or Google Vertex, for clients who forbid third-party APIs but allow their own cloud.

| Location | Default for the models v1 uses | Zero retention |
|---|---|---|
| Claude API, direct | Prompts and outputs not retained by default | ZDR, enabled per organisation; flagged content can still be kept up to 2 years |
| Amazon Bedrock | A retention mode per region, account or project; never shared with Anthropic | Mode `none`, enforceable by AWS organisation policy |
| Google Vertex | Request-response logging off by default; prompt-cache hashes are Google service data | Logging off, and no Advanced AI consent |

- **v1 uses only models and features that allow zero retention at all three:** no Fable or Mythos models (30-day retention at every location); only adaptive thinking with effort, structured outputs and strict tools, and prompt caching; no Batch API (29-day retention), Files API, code execution or server tools. The cost: no batch discount, and not the most capable model.
- **Each engagement declares its policy:** zero retention, or the provider's default. Under zero retention the tool verifies before any call and refuses to run otherwise. On Bedrock and Vertex the promise is **checked**: the effective mode for the pinned model must be `none`; logging must be disabled and no Advanced AI consent granted. On the direct API it is **attested**: a named operator's attestation is recorded, because ZDR cannot be read from the API. Both report documents say which, and when the direct API is used, both state that content flagged by Anthropic's trust and safety systems can be kept for up to 2 years ([ADR 0016][adr16], amended).
- **Residency.** If an engagement names a region, only regional endpoints are used: a region or multi-region on Vertex, never the global endpoint; in-region only on Bedrock, with no cross-region inference; `inference_geo` on the direct API where available. The region is recorded for every call.

**The cost ceiling.** Every run has a required ceiling in money. Spend is computed from recorded token usage and a versioned price table per location, with warnings at 50% and 80%. When it is reached, no new AI step starts and running ones finish; deterministic work carries on (lowering, checks, reconciliation, the consistency pass); each AI step that never started takes its documented fallback and is listed as "skipped: model budget reached". A re-run with a higher ceiling replays every completed call from the cache. The ceiling **binds** when projected remaining spend exceeds the remaining budget; from then on the orchestrator allocates it ([section 11](#11-the-orchestrator-interface)). After the pilot, if projected spend (the pilot's spend per complexity band, times the bulk artefacts in each band) exceeds the ceiling, the tool warns the operator before the bulk starts and points to the orchestrator switch's documented recommendation; it never switches by itself.

**Prompts and models.** Prompts, instruction phrasings and output schemas are files in the tool's repository, versioned with it. A change ships only in a release, gated by the known-good and known-bad suites and by each AI step's measured pass rate on the corpus, which must not fall. Prompts are never edited for one engagement: an engagement adapts only through client lessons. Models are pinned per release; changing one, including when a provider retires it, is a release with the full measurement.

**Refusals.** The tool checks `stop_reason` on every call. On a refusal it retries once through the fallback the location supports (Anthropic's server-side fallback, or the SDK's client-side middleware on Bedrock and Vertex), restricted to the engagement's allowed models (never Fable or Mythos), with the same retention check run on the fallback model first. It records the model actually used in the call record and the cache key. If that is refused too, the step takes its documented fallback, reported as "refused" with the refusal's category.

### 9. Repair

Decided in: [Which autonomy level does each component get?][t14], [What context does each AI step receive?][t21], [What does the testing module own, and how is it kept independent?][t20], [In what order does a run process its artefacts?][t31].

Repair is the only agent outside the orchestrator: a model in a bounded loop that chooses what to look at and what to try.

- **Pinpointed.** It starts from the failing layer and check, the first diverging checkpoint (expected and actual), the minimised failing input and its seed, the region links, and earlier attempts on this failure (each patch with the evidence it produced). It gets no worked examples.
- **Its tools are context-builder queries,** each filtered by the same withholding rules and recorded in the run catalog: the checkpoint at a position; the diff between expected and actual observations; a region of the representation or the generated code; catalog facts; a construct-mapping or silent-difference entry; running the visible checks (layers 1–4 on visible seeds) on a proposed patch.
- **What it may change.** The failing region's representation or generated code. It may change the whole artefact's representation only when the first divergence lies before every region it may change. For a cycle, its scope is the whole unit. Never the support library, the type or error mappings, the import allow-list, the tests, the data generator, the thresholds, or either copy of the original.
- **Bounded.** One repair run per failure, with its own bounded loop and no candidates, always at the top tier. When its context passes the budget, code (never the model) shortens the oldest attempts to "patch summary plus the check it failed". When attempts run out, the artefact is manual work, with repair's evidence in the report.
- **What never goes to repair:** a blocked artefact; a failure classed as a testing-module defect (it does not count as a repair attempt); anything from the final validator, whose results never feed repair.
- **After any repair, checks restart from layer 1.** A failure on a hold-out seed is disclosed to repair in full, those seeds are retired into the visible set, and fresh hold-out seeds are drawn for the next verdict.
- **Repair patches** ([ADR 0008][adr8], amended). When repair fixes a region produced by deterministic lowering or generation rules, the patch is kept as a flagged **repair patch**, keyed by the hash of the region's inputs and reapplied on every regeneration while those inputs are unchanged. A changed input drops it, and the region is converted and checked afresh.
- **A successful repair drafts a lesson** ([section 13](#13-lessons)); for a region produced by deterministic rules it also drafts a kind-5 lesson, a proposed rule, so the rule is fixed in a release.

### 10. A run: plan, phases and dependencies

Decided in: [In what order does a run process its artefacts?][t31], [How does the final independent validator work?][t32]. ADRs: [0012][adr12], [0013][adr13].

**The stages of a run:**

1. Pin the snapshot.
2. Lower every artefact, and describe procedure result shapes in the sandbox.
3. Detect clusters.
4. Compute the **run plan** and record it in the run catalog.
5. The **pilot** phase, ending with the pilot validated as a delivered project.
6. The **bulk** phase.
7. The **consistency pass**, then final validation of the whole project, then one short final step for any artefact that fell back from a raised form to its faithful form.
8. The report.

Lowering everything first is cheap and deterministic, and gives the plan what it needs: effect summaries, the constructs present, and the structure clusters are found from.

**The run plan** is a partial order with phase boundaries, computed by code before any conversion. Both orchestrators must keep it; within it, the orchestrator chooses which ready item to take next, and the report records the order actually taken.

**Dependencies.** An artefact's *verification* waits for every artefact whose converted code that verification runs: a procedure or function it calls; a view or function it reads; an enabled trigger on a table it writes. External references are ignored. *Conversion* of a caller does not wait: callees' signatures, effect summaries and result shapes come from lowering and the sandbox. When a callee's conversion changes later (a lesson, a revocation, repair), the artefacts depending on it are verified again in the consistency pass.

**When a dependency hasn't converted.**

- **"Blocked by a dependency"** names the callee. A caller is blocked whenever a callee has not reached "Reconciled", for any reason. It is not converted, is not sent to repair, and is verified once the callee reaches "Reconciled". In final validation, a caller whose callee failed or was not validated is "Blocked by a dependency", whatever its own result.
- **A callee with only a recorded acceptance** ([ADR 0006][adr6]): the caller may be verified against it and may reach "Reconciled", but carries the flag **"relies on an accepted dependency"**, naming the callee, and does not qualify as a worked example.

**Cycles.** Each strongly connected component (recursive procedures, procedures calling each other, triggers writing back to tables with triggers) is one unit: its members are converted in any order, verified together with every member's conversion in place, blocked together, and repaired as one. Self-recursion is a unit of one.

**Dependencies not fully known.** Artefacts with dynamic SQL, a scope barrier or an unresolved reference are verified last in their phase, their report says the dependency list is incomplete, and they are never chosen for the pilot.

**Choosing the pilot.** A greedy set cover over the constructs in the lowered representation: node kinds, operations, candidate raising transforms, and the features of the triggers involved.

- A smaller artefact is preferred when it adds the same coverage; any remaining tie is broken by a hash of the name, so the pilot is reproducible.
- At most one representative per cluster. Every callee of a pilot artefact is added, so the pilot can be verified.
- A versioned size cap, by default 15% of the artefacts. If the cap is reached first, the constructs left uncovered are listed in the plan.

**Phases and lessons.**

- **The bulk waits for the pilot:** every pilot artefact has a verdict, every candidate lesson from the pilot is confirmed or rejected, and the pilot has passed or failed final validation. In a preview run the barrier is only that every pilot artefact has a verdict, and lessons drafted stay candidates for the next full run.
- **A lesson confirmed during the bulk** applies at once to artefacts not yet processed. Bulk artefacts already processed that it matches are converted again once, in the consistency pass. The confirmation gate re-runs the lesson's own artefacts at once.
- **The consistency pass** freezes the lesson set and re-verifies every artefact under it ([section 13](#13-lessons)).

**Subsets, parallelism and isolation.**

- An operator may restrict a run to a subset. The planner adds what verifying it needs (callees, triggers on the tables it writes), marked as added. The pilot is drawn from the subset, and the consistency pass covers only the run's set.
- **Parallelism** is a setting, the same for both orchestrators.
- **Isolation:** each verification gets its own scratch database and its own Spark session ([Reconciliation](#reconciliation)), so parallel verifications never see each other's writes.

### 11. The orchestrator interface

Decided in: [What orchestrator interface lets an agent and a plain pipeline be swapped?][t15], [What makes an agent loop checkpointed and replayable?][t5]. ADR: [0020][adr20].

**An orchestrator is a policy over a fixed engine.** It sees an observation of the run and returns actions, which the engine runs, checks against the plan and limits, and records. The agentic orchestrator (a model in a loop calling the actions as tools) and the plain-pipeline baseline (ordinary code) implement the same interface and drive the same components unchanged. Which one is the default is decided by the build's first experiment ([Build Commitments](#build-commitments)). Both stay in the product behind one switch, the only switch in how the tool uses models.

**Fixed by the engine, the same for both:** the run plan, its phases and partial order; the fixed sequence of steps for each artefact; attempt limits, the budget per artefact and the run's cost ceiling; model routing, and escalation inside a step (checker failures and failing candidates); candidates and the rule that picks one; repair's own loop, and that checks restart from layer 1 after it; the final validator's call points; the consistency pass and lesson drafting; the parallelism setting; that a blocked artefact never goes to repair.

**What an orchestrator chooses:**

- (a) **order:** which ready item or unit to take next, within the run plan;
- (b) **failures:** on a failure found after a whole step, whether to retry one tier up, send the item to repair, or give up, within the limits. `retry_up` re-runs the AI step behind the failing region one tier up; the engine refuses it for regions produced by deterministic rules, and at the top tier;
- (c) **giving up early,** before the limits are reached;
- (d) **requeueing** an item it gave up, or a blocked one, when new information arrives (a lesson confirmed, a callee converted, a similar artefact succeeding). Requeueing never resets attempt counters; an item that used up its attempts gets another chance only through a confirmed lesson matching it, which the consistency pass converts again;
- (e) **budget allocation:** once the cost ceiling binds (projected remaining spend exceeds the remaining budget), `allocate` sets each artefact's maximum, within its own budget.

It never skips a step, raising included. The cost of so narrow a choice is deliberate: a wider one would let an orchestrator win on speed by skipping work that "converted" doesn't measure.

**Actions:** `advance(item)`, `retry_up(item)`, `repair(item_or_unit, failure)`, `give_up(item, reason)`, `requeue(item)`, `allocate(budget_shares)` and `wait`. An orchestrator returns up to N actions, N being the parallelism setting. The engine refuses any action that breaks the plan or a limit, and records the refusal.

**What it sees, and never sees.**

| Shown | Never shown |
|---|---|
| Each item's status, last step, failing layer and dimension, attempts and tiers used, complexity score, cluster and structural-pattern identifiers, cost so far; the run's budget left, elapsed time and ready count | Source text, generated code, failing inputs, seeds of any kind, the final validator's evidence (only its pass or fail) |

The agentic orchestrator's own model calls follow [ADR 0009][adr9] like every other call: assembled by the context builder, cached, checked for retention, and run at the top tier, versioned with the tier table. They draw on a reserved share of the run's ceiling, 5% by default and versioned; when that is spent, the agentic orchestrator falls back to the baseline's policy for the rest of the run ([ADR 0020][adr20], amended).

**The plain-pipeline baseline,** scripted as a careful engineer would: take ready items in run-plan order, pilot first, smallest complexity score first; on a failed check, retry one tier up until the top tier, then repair, then give up at the limit, never early; requeue a blocked item when its callee converts; when the ceiling binds, allocate the budget in run-plan order.

**A run keeps one orchestrator throughout,** so its decisions stay replayable. An operator who switches stops the run and starts again with the other orchestrator; every completed model call replays from the cache.

### 12. The run catalog, caching and resuming

Decided in: [What makes an agent loop checkpointed and replayable?][t5], [What orchestrator interface lets an agent and a plain pipeline be swapped?][t15], [What context does each AI step receive?][t21], [What does the catalog hold and how does conversion read it?][t8]. Research: [replayable agent loops][r-replay].

- **A model's decision cannot be regenerated, only recorded and read back.** Identical inputs may produce different outputs, and the models used reject sampling parameters. So replay comes from the run catalog, never from asking again: an agentic run is resumable and replayable, but a fresh run is not reproducible.
- **The run catalog records,** per artefact, append-only and in order: content hashes of every input a step read; the model sent and the model that answered; the prompt version; each model response exactly as returned, including tool calls; each tool call's input and result; the run plan; every orchestrator action, as intent before it runs and with its result after; the representation documents; hashes of produced files; transaction start versions; scratch tables to clean up.
- **Idempotent steps.** Results are stored by a hash of their inputs. A crash between doing the work and recording it re-runs the step and gets the same stored result: execution is at least once, the effect exactly once.
- **Resuming** an interrupted run replays its recorded decisions, including the agent's model responses, up to the interruption, then continues live.
- **Caching.** Every cache key includes the representation format version, the catalog facts read, the context builder version, the prompt and model versions, the candidate variant, tier and attempt, the library version and the test definition's fingerprint, as the relevant step requires. On a warm re-run the plain pipeline's decisions are deterministic; the agent's calls hit the cache only when their whole context matches, so an agent's steps cache poorly across runs.
- **Prompt caching** at the provider is a discount on a repeated prefix, not a result cache. The result cache is the tool's own, in the engagement's control database.

### 13. Lessons

Decided in: [How does the tool learn from its mistakes, and how are earlier conversions brought up to date?][t23], [How does a reviewer's fix get back into the tool?][t16] (folded into it). ADRs: [0010][adr10], [0011][adr11].

**A lesson** is a recorded correction applied to later conversions: a lesson pattern, the symptom it answers, a fix, its evidence and a status. A reviewer's fix is a lesson with a human source, through the same mechanism and the same gate. Code does every part: drafting, generalising, confirming and promoting. No model decides a lesson's pattern or its confirmation, so ADR 0008's five places are unchanged; repair's one-line hypothesis, written by a model, is kept in a client lesson as evidence only.

**Where lessons come from.**

- **From a successful repair, drafted by code:** the lesson pattern is the repaired region's pattern with every identifier and literal replaced by a placeholder, matched exactly; the symptom is the failing check's evidence; the fix is the repaired region as a worked example, with repair's one-line hypothesis.
- **By generalisation:** when two or more drafted lessons share a fix kind and a failure class (the failing layer, check and data-quality dimension) and their lesson patterns differ only at some nodes, code computes their least general generalisation (anti-unification), with typed wildcards at the nodes that differ. A generalisation is rejected before the gate if its root is a wildcard or it fixes fewer than half of the fixed nodes in the smaller member's lesson pattern (a versioned threshold). It runs within one scope only, each time a lesson is drafted, never during the consistency pass, and for fix kinds 1–4 only. Once it is confirmed, its members are superseded; if it is later revoked, they return to confirmed and the retroactive search runs for them.
- **From a reviewer,** written by hand, who may add wildcards to a lesson pattern.
- **Not lessons by themselves:** faulty-rule reports, failures that ran out of repair attempts, and lesson conflicts are listed as open problems for a reviewer.
- **Lessons learnt on the sample and public corpora are global from the start,** since they hold no client code.

**The lesson pattern** is a fragment of representation whose leaves may be typed, numbered placeholders for identifiers and literals; typed wildcards (any expression of a type, any list of statements, any predicate); and optional constraints on type, collation and nullability. Everything else matches exactly. Specificity is the number of nodes it fixes. Matching is always exact and structural, never by vector similarity.

**What a fix can be.**

| Kind | Fix | Scope | Effect |
|---|---|---|---|
| 1 | Context note | Either | Text for the AI steps whose region the lesson pattern matches |
| 2 | Worked example | Either | A corrected input and output pair, shown to those steps |
| 3 | Force the faithful form | Either | Disables a raising transform where it matches |
| 4 | Mark as manual work | Either | For a rule's mistake that can't be fixed during a run; a rule fix is proposed for the next release |
| 5 | Proposed rule or helper | Global | For the maintainers; no effect during a run |
| 6 | Region replacement | Client, one artefact | Target code a reviewer supplies for one region, regenerated into the output each time, tested like any other code, flagged as reviewer-supplied ([ADR 0011][adr11]) |

No lesson of any kind can change the type mapping, the handling of silent differences, prompts, or how tests judge. A lesson may add test inputs; they are part of the engagement's test definition and enter the fingerprint as a hash. A mistake in a deterministic rule is fixed by a tool release; during a run, a lesson can only route the region to its faithful form or to manual work.

**The confirmation gate.** A lesson goes from candidate to confirmed when:

- every artefact it came from, and for a generalised lesson every artefact it matches, reaches "Reconciled" with the lesson applied, and passes final validation (only the pass or fail result is used);
- the **known-good suite** still passes: for a global lesson, the reconciled sample and public corpora outside the release hold-out; for a client lesson, those plus the client's own reconciled artefacts. Only artefacts the lesson pattern matches are re-run.

For kinds 1 and 2, whose effect goes through an LLM step, the first condition must hold for every candidate variant. A candidate lesson applies only to its own artefacts until confirmed. The wrong output a lesson fixed is kept as a **known-bad case** the tests must keep rejecting: a client lesson's case stays in that client's control database; a global lesson's case is rebuilt on a corpus artefact the lesson pattern matches, and without one the lesson is not confirmed. The new test inputs a lesson needs are generated blindly from the original, aimed at the lesson pattern's region; inputs a reviewer writes join the visible set only.

**Applying lessons.** Confirmed lessons whose lesson pattern matches the region fill the context builder's applicable-lessons slot, ordered by specificity, client lessons first when specificity is equal. The slot is in every candidate's context and is never trimmed. Between two matched lessons whose fixes conflict, the more specific wins; with equal specificity neither applies and the conflict goes to a reviewer. A lesson that changes a shared conversion re-instantiates and re-tests every instance.

**Statuses and revoking.** Candidate, then confirmed, then superseded (promoted to a rule, or covered by a confirmed generalisation) or revoked. Revoking re-runs the retroactive search, and every matched artefact is converted again without the lesson. A revoked lesson is kept with its reason, never deleted, except with its client's data.

**Where they live, and who writes them.**

- **Global lessons** live in a lesson store the vendor holds, released with the tool as the global lesson set, never stored in an engagement. A new global lesson reaches an engagement only when it next runs on the release containing it.
- **Client lessons** live in the engagement's control database. A run pins both lesson-set versions.
- **Promoting a client lesson to global:** a deterministic check, run inside the engagement because it needs the client's catalog and source, verifies the rewritten lesson holds no client code. It rejects any identifier from the client's catalog (object, column, parameter and other names) wherever it appears, and checks string literals of 4 or more characters, and any run of 8 consecutive tokens from the client's source (after removing comments and whitespace), excluding runs made only of vocabulary names. Placeholders are typed nodes in representation and `⟦kind:n⟧` in text. All three numbers are versioned settings. Only the rewritten lesson leaves, without repair's hypothesis, and it is confirmed again against the global known-good suite.
- **Authors:** in v1, delivery engineers write client lessons; later, a client's staff may write client lessons only. Only maintainers create, promote and revoke global lessons; promoting a lesson to a rule or helper is a tool change in a release.

**The consistency pass and earlier conversions.** Every run ends with a consistency pass that freezes the lesson set and re-verifies every artefact under it; candidate lessons arising during it are recorded for the next run. The retroactive search finds every artefact a lesson, revocation, retired example, rule version or helper version affects. The report gives "changes since the last run", an impact list per lesson, and the lessons applied at each construct with their scope, source, date and status.

### 14. The control database

Decided in: [What does the catalog hold and how does conversion read it?][t8], [How does the final independent validator work?][t32], [What context does each AI step receive?][t21]. ADR: [0007][adr7].

- **One Postgres database per engagement** is the source of truth for everything the tool persists for that engagement: catalog snapshots (immutable rows with content hashes), the name-adjustment record, the run catalog, stored observations and verdicts, client lessons and known-bad cases, the business-rules file, descriptions, the model-call cache and rendered contexts, and later embeddings.
- **Deleting a client's data is dropping their database.** No query can cross clients by mistake.
- **The final validator has its own schema** in it, under its own role, which the engine's role cannot read. Validator replay uses only that role. The validator publishes, where the engine can read it, only each artefact's pass or fail and a defect notice with no failing input.
- **Global lessons do not live there;** they ship with the tool.
- **A file export** of a snapshot exists for comparing snapshots with ordinary diff tools. It is derived, never the source of truth.
- **Vector similarity** (pgvector) comes later and is used only to propose similar artefacts, never to decide which artefacts a lesson affects.
- **Every run needs Postgres and the sandbox SQL Server,** including a preview run, because procedure result shapes are described in the sandbox.

### 15. The conversion report

Decided in: [What does the conversion report look like?][t9], with requirements posted to it by most other tickets. ADRs: [0006][adr6], [0008][adr8] (amended), [0012][adr12]. Prototype: [`prototype/report/`](../../prototype/report/).

**Each run produces one deterministic report data file** (`report-data.json`, with a versioned schema, whose run-specific fields (times, durations, cost, audit-sample results) sit in a section of their own) **and two documents rendered from it, each as HTML and PDF.** Every sentence in both is a fixed template filled from the data. The only AI-written text is the intent summary, shown only when a deterministic check passes. English only.

**Rendering.** A standard-library renderer turns the data into HTML, byte-identical across renders. WeasyPrint (BSD) turns the same HTML into PDF, with fonts and the renderer version pinned and the document dates set from the run's finish time (`dcterms.created`, `dcterms.modified`, `SOURCE_DATE_EPOCH`); without that, a PDF carries its render time. The two formats can differ only in layout. The sign-off PDF is PDF/A, with free fonts embedded.

**The client sign-off document,** a one-page verdict letter for a non-technical client and a business analyst:

- a headline sentence and a bar;
- four groups by what the reader is asked to do: converted and checked; not converted yet (our work); waiting on another item; manual work;
- what we need from you (for example widening the extraction, exporting identity starting values);
- issues found in the existing system (pre-existing issues);
- the single-writer condition, stated once as "at most one writer per table at any moment, converted or not, across every project in your environment", with what the tool enforces (write leases between converted jobs) and what it cannot (other writers);
- one signature block.

A table of business areas is added when a run spans several. At scale, each group is summarised by count and capped at 20 items, pointing to the engineering report.

**The engineering report,** register first, for engineers: the run header (versions, builds, retention promise, model use); every item that isn't clean, worst first, with its reason and next step; the registers (degradations, changes since the last run, open problems, deployment steps); every artefact in full as an appendix, with verdict and validation side by side. Its HTML is an index page plus one static page per business area, with no scripts; its PDF is one document with bookmarks per area and artefact.

**Status vocabulary.** An artefact's status is derived only from its verdict and its validation result:

| Verdict | Validation result | Status a reader sees |
|---|---|---|
| Reconciled | Passed | Converted |
| Reconciled | Failed | Failed final validation |
| Reconciled | Not validated | Not validated: (reason) |
| Reconciled | Blocked in validation by a callee that failed or was not validated | Blocked by (callee) |
| Reconciled | Not yet run (for example a run interrupted before final validation) | Awaiting final validation; not converted |
| Reconciled, weakly tested | Not run | Weakly tested |
| Failed | Not run | Failed: (dimension) |
| Blocked by a dependency | Not run | Blocked by (callee) |
| Blocked by a testing-module defect | Not run | Blocked by a testing-module defect |
| Not reconcilable locally | Not run | Not reconcilable locally: (reason) |
| Not reconcilable: incomplete observation | Not run | Not reconcilable: incomplete observation |
| Not testable: contains manual work | Not run | Contains manual work |
| (recognised artefact type) | Not run | Recognised, not converted |
| Not reconciled (preview run) | Not run | Not reconciled |

**Flags** sit beside a status, never replacing it: relies on the single-writer assumption; differs from the original after an error (ADR 0004; shown in the sign-off); pre-existing issue; relies on an accepted dependency; recorded acceptance; raised form; fell back to the faithful form; reviewer-supplied region; repair patch.

**Sign-off groups,** from each status:

| Status | Sign-off group |
|---|---|
| Converted | Converted and checked |
| Failed, Failed final validation, Weakly tested, Blocked by a testing-module defect, Not validated, Not reconcilable: incomplete observation, Awaiting final validation | Not converted yet (our work) |
| Blocked by (callee) | Waiting on another item |
| Not reconcilable locally, where widening the extraction or providing a remote tier would fix it | What we need from you |
| Not reconcilable locally otherwise; Contains manual work; Recognised, not converted | Manual work | **Construct statuses:** converted, or converted with a caveat; raised; manual work; failed; blocked. A construct may also be tagged as produced by a model, as using the escape hatch, or as reviewer-supplied. **Rollup:** construct, artefact, business area (the schema by default, mappable per engagement), run. Recognised artefacts count in the totals as manual work.

**Wording.**

- "Converted and checked" reads: "on every test we ran, the converted code behaved like the original, and an independent second check agreed, except where flagged." It never claims equivalence in general. An item flagged "differs from the original after an error" shows the flag beside it in the sign-off ([ADR 0012][adr12], amended).
- **The retention promise** says whether it was checked (Bedrock, Vertex) or attested (direct API), and on the direct API states that flagged content can be kept for up to 2 years.
- **The plain-language line per artefact** in the sign-off is its intent summary, footnoted as AI-written and context only, and shown only if every table it names is in the artefact's read and write set and its action verbs, from a fixed list, match the artefact's effects (creates, adds, inserts: insert; updates, changes, sets: update; deletes, removes: delete; reads, returns, lists: read only; other verbs are ignored, and a summary claiming only reads fails if the artefact writes). Otherwise a template line built from the generated description is shown. The engineering report shows both, each labelled.
- ISO dates; object names exactly as in the source; a preview run's banner as the first line of both documents; marked secrets masked wherever code appears; the final validator's failing inputs never shown, only its defect notice.

**What signing means.** Signing accepts the items listed under "Converted and checked", for go-live, on the stated condition, and nothing else; the descriptions are not part of what is accepted. A not-converted item can be accepted only through a recorded acceptance for that artefact ([ADR 0006][adr6]), shown beside its verdict in both documents.

**No degradation without a place in the report** ([ADR 0012][adr12]). The report has a visible place for every skipped check, fallback, assumption, trimmed context, unreachable branch and reduction in coverage; a component with no place to record a degradation may not degrade. Requirements posted by other tickets land as follows. In the sign-off: the groups, pre-existing issues, the single-writer condition, what the client must do, the preview banner, recorded acceptances. In the engineering report, everything else:

- failures by data-quality dimension with their evidence;
- coverage from both judges, never merged;
- mutation results;
- tracing availability;
- exclusions and caveats;
- the degradations register;
- changes since the last run and open problems;
- lessons applied and region replacements;
- the run plan and the order taken;
- model tiers, budget, refusals, the retention promise and residency;
- versions and builds;
- the client's build and the sandbox's;
- version differences and pre-2005 syntax;
- deployment steps and defect advisories;
- client defects and incomplete observations;
- external databases whose extraction would make artefacts reconcilable, with the count each would unlock.

### 16. SQL Server versions

Decided in: [Which SQL Server versions does v1 support?][t26], [Which behaviours differ between SQL Server 2008–2016 and a newer engine at the same compatibility level?][t33]. ADR: [0015][adr15]. Annex: [E](../research/sql-server-version-differences.md).

**v1 supports SQL Server 2000 to 2025, routed by each database's compatibility level.** Every SQL Server with a Linux container image accepts levels 100 and above only (tested: 2017 accepts 100–140, 2019 100–150, 2022 100–160, 2025 100–170).

| Client server | Compatibility level | Sandbox | Parser |
|---|---|---|---|
| 2017, 2019, 2022, 2025 | 100 or above | A container of that version, pinned to one CU | ScriptDom's parser for that level |
| 2008, 2008 R2, 2012, 2014, 2016 | 100 or above | A 2022 container at the client's level, with the known version differences listed | ScriptDom's parser for that level |
| Any | 80 or 90 | A guarded source-side remote tier only; otherwise "Not reconcilable locally" | `TSql80Parser` or `TSql90Parser` |

- **Pinned builds.** Each version's image is pinned to one CU and versioned; the report shows the client's build and the sandbox's.
- **The sandbox must run at the client's exact level:** crossing level 130, for example, changes every `datetime` to `datetime2` conversion.
- **What a 2022 container does not reproduce** from 2008–2016 is catalogued in Annex E: 19 removed features that fail at every level (an artefact using one is "Not reconcilable locally"), 16 changes no level covers, and 31 a level does cover. Of 29 level-gated behaviours tested, 28 matched. **RC4 encryption at level 100** creates and opens keys without error but `ENCRYPTBYKEY` returns `NULL`, so such artefacts are "Not reconcilable locally". The parser flags any artefact using a listed construct, and the report lists it.
- **Windows-only features** make an artefact "Not reconcilable locally" unless a Windows remote tier of that version exists: `EXTERNAL_ACCESS` and `UNSAFE` CLR; FILESTREAM and FileTable; `xp_cmdshell` and other extended procedures; distributed transactions; linked servers to providers other than SQL Server. The report names the feature.

**Source-side remote tiers** ([ADR 0015][adr15], amended): a dedicated test server receiving synthetic data only, never production data, for levels 80 and 90 and for any level when Windows-only features need one. The guard applies to every source-side remote tier, and the tool refuses to run unless every check passes:

- a **tester login**, never the extractor's, holding no `sysadmin` or other server-level role (`dbcreator` included), granted `CREATE ANY DATABASE` directly (on 2000, `CREATE DATABASE` in `master`), and keeping `VIEW ANY DATABASE` so the database list it sees is complete;
- the server holds no databases other than system databases and the tool's own scratch databases;
- the server's identity (`@@SERVERNAME` and its service identifier) differs from the server the snapshot came from.

**Extraction from older servers.** 2017–2025: the decided grant set, tested. 2008–2016: the same grant set, by documentation until the VM tests. 2005: the same, with the dependency cross-check on `sys.sql_dependencies` or skipped. 2000: system tables captured wholesale, database access only, and the client is told that on 2000 any database user can read definitions.

**Types and syntax added for old versions.** `text`, `ntext` and `image` map to string and binary; text-pointer statements are manual. `timestamp` (`rowversion`) maps to `binary(8)` from a database-wide counter, compared by property by both judges. Pre-2005 syntax:

| Syntax | Handling |
|---|---|
| `*=` and `=*` joins | Converted to ANSI outer joins (a predicate on the inner table moves into `ON`), flagged; verified only on a remote tier at level 80, otherwise "Not reconcilable locally"; the faithful form is still produced |
| `COMPUTE`, `COMPUTE BY` | Manual work |
| The old `RAISERROR n 'text'` form | Converted |
| `GROUP BY ALL` | Converted |
| Ordered `TOP 100 PERCENT` views | Converted without the order, with a caveat that a 2000-level server honoured it |

**Not supported in v1:** versions before 2000, Azure SQL Database and Azure SQL Managed Instance. Extraction from an unlisted version stops with a clear error; extraction from folders still works with the version declared. Until a version passes the build's VM tests, the report says "support for this version rests on documentation".

### 17. Sources and targets

Decided in: [What must a new source or target provide?][t17], [Which parsers can read T-SQL, PL/SQL and SSIS packages well enough to build on?][t2]. ADRs: [0001][adr1], [0019][adr19]. Annex: [B](../spec-drafts/source-target-contract.md).

**Each source and each target implements a fixed contract** (Annex B gives it in full). A source provides an extractor, extension fields and a raw capture; a name resolver, parsers per version, and lowering; secret patterns and a version policy; the mapping from error classes to its own codes; the source halves of the differences catalogue and the type mapping; a runner, tracing and coverage; two complete clients sharing no code, one per judge, and per-statement row counts from the server; the validator's own path; a vocabulary list for lessons. A target provides generation and an unsupported list; the target halves of the type mapping (with published representation rules) and the differences catalogue; the mapping from its runtime's failures to error classes; a support library; an entry-point manifest; layer-1 code checks, expression extraction and mutation operators with the helper-to-built-in mapping; a tracer; sandbox, remote-tier and validator runners; optionally a second renderer; a vocabulary list for lessons. Both declare the vocabulary version they implement and what they add, and never change an existing meaning. A target that cannot handle an operation or represent a type declares it unsupported, and anything using it is manual work on that target ([ADR 0001][adr1], amended). Both use free tools or recorded exceptions. A source's extractor keeps all six promises of [section 3](#3-extraction-and-the-catalog).

**Mappings shared by a source and a target are split** into a source half and a target half that meet at the vocabulary ([ADR 0019][adr19]), so S sources and T targets need S + T of each, not S × T:

- **Error identity:** the core's neutral **error classes**; targets map failures to classes, sources map classes to codes. `raise_error` keeps its SQL Server meaning; Oracle adds `raise_exception(code, message)`.
- **The differences catalogue:** Annex C (SQL Server half) and Annex D (PySpark half).
- **The type mapping:** both halves are in [section 6](#6-converted-code).

**Testing-module rules that were Python's** belong to the PySpark target: layer 1's checks, the mutation operators and how they are applied, and how an expression's span is cut out. The core keeps the layer order, the rules and the verdicts. A target with no runtime hook traces an instrumented test copy of its delivered code, under the same equivalence check as the original's, and never ships it. The support library is per target, in that target's unit of packaging ([ADR 0018][adr18], amended).

**Parsing SQL Server.** Microsoft ScriptDom (MIT, .NET) through pythonnet, the only candidate that parsed every procedural construct into a typed tree with positions and comments; one parser per compatibility level. sqlglot is usable for query-level statements but not as a foundation for procedures. SSIS packages are read as XML with the standard library.

**Oracle PL/SQL, on paper.** Everything fits as an addition: types (records, collections, Oracle `NUMBER`, intervals, timestamps with a named time zone, `ROWID`), node kinds (packages with session state; triggers parameterised by timing and level), and operations (among them `assign_exactly_one_row`, `SQL%ROWCOUNT`, collection operations, `BULK COLLECT` and `FORALL`, `raise_exception`, `autonomous_block`, `hierarchical_query`, empty string as null). Wrapped source and the `MODEL` clause are recognised only. Parsing with the grammars-v4 PL/SQL grammar or sqlfluff; clients `python-oracledb` (thin mode) and Oracle's JDBC driver (a recorded exception); sandbox Oracle Database Free 23ai (a platform exception), with 19c and earlier on a guarded source-side remote tier unless shown faithful.

**The cross pairings, on paper.** Annex B also walks Oracle PL/SQL onto the PySpark target and SQL Server onto Snowflake. Among its results: Oracle's `NUMBER` with no declared scale and `ROWID` are declared unsupported by PySpark; collections live in session state; an autonomous block that writes a table its enclosing transaction also writes is manual. On Snowflake, converted procedures run with caller's rights and keep session state in a session-scoped temporary table.

**Snowflake SQL, on paper.** Everything fits as a target responsibility: the write path checks keys and foreign keys and emulates triggers (Snowflake enforces only `NOT NULL`); identity values from counters in a state table; savepoints declared unsupported or emulated; errors mapped to error classes, with one handler block per statement where ADR 0004 needs execution to continue; several result sets packaged by a convention the entry-point manifest describes; the support library as a schema of SQL functions and procedures vendored under a versioned schema name; an instrumented test copy with checkpoints in event tables; the Python connector and JDBC as clients (Apache 2.0). There is no local engine, so a Snowflake account is the remote tier where layers 2–6 and the final validator run.

### 18. Tools, licences and client promises

Decided in: [ADR 0017][adr17] (the owner's free-tools rule), [Observe the ODBC driver after a failed statement][t34], [Can a SQL Server container and local PySpark stand in for the real platforms?][t4], [What does the catalog hold and how does conversion read it?][t8], [How does a run govern its model use?][t30].

**Every tool the product uses is free, and open source wherever possible; the Claude models are excepted.**

| Need | Tool | Licence |
|---|---|---|
| Language | Python 3.12 | Open source |
| Target runtime in the sandbox | PySpark and Delta Lake, matched to the client's declared Databricks Runtime: 17.3 LTS (Spark 4.0.0, Delta 4.0.0, JDK 17, Python 3.12) or 18 LTS (Spark 4.1.0); one image per supported runtime | Apache 2.0 |
| Control database | Postgres, later pgvector | Open source |
| T-SQL parsing | ScriptDom on .NET, through pythonnet | MIT |
| Query-level SQL | sqlglot | MIT |
| Constraint solving | Z3 | MIT |
| SQL Server client, runners and extractor | Microsoft.Data.SqlClient through pythonnet, with `FireInfoMessageEventOnUserErrors` on | MIT |
| SQL Server client, final validator | mssql-jdbc | MIT |
| Fallback clients for old servers | FreeTDS (runners and extractor), jTDS (validator) | Open source |
| PDF rendering | WeasyPrint, pinned, with free fonts | BSD |
| Containers | Docker Engine or Podman; the product never depends on Docker Desktop | Open source |

- **Recorded exceptions** are the platforms themselves: SQL Server, with the Developer edition as the oracle; Databricks (the client's target, the optional remote tier and the vendor's release-test workspace); Windows VMs with licensed media for the build's tests of SQL Server 2000 to 2016. Any new tool meets the rule or is added to the exceptions with its reason.
- **Why SqlClient and mssql-jdbc.** After a failed statement the server carries on from, `pyodbc` with Microsoft's ODBC driver and `pymssql` both lose the later result sets, row counts and return values; SqlClient with errors delivered as messages, and mssql-jdbc reading on after each error, return them all (tested). So the two judges each have a complete client and share none. Each cross-checks its client against Extended Events' per-statement row counts from the server; a mismatch is a client defect, reported, and the artefact is not converted. Server counts can include rows written by nested statements such as triggers, and each judge's self-test confirms the mapping on known cases.
- **Fallbacks.** Where a fallback client loses results after an error, the observation is incomplete: "Not reconcilable: incomplete observation" or "Not validated: incomplete observation". jTDS is unmaintained and used only against the isolated old-version test servers, never a client's production server or the extracted server.
- **The ODBC driver** stays in the toolbox image only as the test record; `pymssql` stays in the corpus tools only.

**Client promises,** collected:

- **The extractor** is read-only, never executes client code, never reads a row of client data, never holds `EXECUTE`, and checks its own permissions ([section 3](#3-extraction-and-the-catalog)).
- **Synthetic data only.** The sandbox and every remote tier receive synthetic data generated from the catalog and the original, never client data. Profiling the client's real data and shadow runs on real data are out of scope.
- **No security metadata** is stored, and **marked secrets** are masked before any model or report sees them and never written into generated code.
- **Zero retention** can be promised at every model location: checked on Bedrock and Vertex, attested on the direct API, with the direct API's 2-year exception for flagged content stated ([section 8](#8-ai-steps)).
- **One control database per engagement,** dropped to delete a client's data; client lessons and examples never leave it; global lessons hold no client code by a deterministic check.
- **The single-writer assumption** is the one condition the converted code places on the client's operations: at most one writer per table at any moment, converted or not, across every project in the environment. Converted jobs enforce it among themselves with write leases ([ADR 0021][adr21]).

## Testing Decisions

### What makes a good test here

- **SQL Server execution of the original is the only oracle.** A test passes when the conversion behaves like the original on the same inputs, never when it matches someone's expectation.
- **Tests judge behaviour, not code.** The testing module compares observations: result sets, output parameters, return values, row counts, errors, messages, and the after-state of every table written, directly or through triggers and foreign-key actions. It needs nothing of the representation beyond the links from code back to source positions.
- **The enemy is coincidental correctness:** wrong logic giving the right answer on the test data. Discriminating data, tracing, expression checks, mutation testing, hold-out seeds and an independent second judge all exist to catch it.
- **Independence over convenience.** Nothing that decides a pass is shaped by the conversion, and the second judge shares no code with the first.
- **No silent weakening.** No component may make a check pass by weakening it, and every exclusion, tolerance and caveat is listed in the verdict.

### The testing module

Decided in: [What does the testing module own, and how is it kept independent?][t20], with later changes from [How does reconciliation generate data and compare results?][t13], [How do the inner checks work: statement tracing, expression checks and mutation testing?][t24], [In what order does a run process its artefacts?][t31], [Observe the ODBC driver after a failed statement][t34] and [What acceptance criteria make the five selling points checkable?][t19]. ADRs: [0005][adr5], [0006][adr6], [0012][adr12], [0013][adr13].

The **checker** verifies the representation and is not part of the testing module. The **testing module** judges generated target code only, in six layers that run cheapest first and stop at the first failure.

| Side | Supplies |
|---|---|
| Core | The verdict format, the order of layers, comparison, the data generator, the independence rules |
| Each source | Runners that execute the original in the sandbox and return an observation; the instrumented copy for tracing |
| Each target | The code checks, the static check, a runner that executes converted code and returns an observation |

Neither runner compares anything.

**The six layers.** Layers 3 to 6 need SQL Server and run only when reconciliation is on, which is the default.

1. **Code checks:** the file parses as Python; lint with a fixed rule set (undefined names, unused variables, unreachable code); imports only from an allow-list (PySpark, Delta, the support library at the pinned version, other converted artefacts, a small fixed set of standard modules); ANSI mode pinned on; every write through session state; every `spark.sql` accounted for as a flagged escape-hatch use; every piece of code linked to source positions and every opaque fragment and manual item accounted for; every linked expression one self-contained span.
2. **Static check:** a dry run on empty tables built from the catalog, recording which statements were reached; parameters without defaults get generated values; only errors showing the code is broken fail it; the output shape is compared with the original's where SQL Server could describe it, and otherwise the comparison is skipped and the verdict says so.
3. **Expression-level differential checks** ([The inner checks](#the-inner-checks)).
4. **End-to-end reconciliation with statement-level tracing, on visible seeds.**
5. **The same on hold-out seeds.**
6. **Mutation testing,** which judges the tests, not the code, so it runs only after the code has passed.

**Verdict outcomes.**

| Outcome | Meaning |
|---|---|
| Reconciled | Every layer passed on hold-out seeds. Carries where it ran (sandbox or remote tier), coverage of the original, seeds, the mutation result, and whether tracing was available |
| Reconciled, weakly tested | Passed, but surviving mutants or coverage of the original fell short of a threshold. Not converted |
| Failed | A layer failed. Evidence: layer, check, data-quality dimension, source positions, the minimised failing input and its seed, expected and actual at the first diverging checkpoint |
| Blocked by a dependency | A callee has not reached "Reconciled" |
| Blocked by a testing-module defect | A business invariant breaks on the converted side only while reconciliation passes, or a client disagrees with the server's row counts. Never sent to repair, never a repair attempt; a fixed defect gives a new verdict under the new fingerprint |
| Not testable: contains manual work | Lists the manual items. Code checks still run on what was generated; there is no partial reconciliation |
| Not reconcilable locally | It cannot run in the sandbox, with the reason, and no remote tier ran it |
| Not reconcilable: incomplete observation | A fallback client lost results after an error on a deciding input |
| Not reconciled | A preview run: reconciliation was off |

Flags beside a verdict: pre-existing issue; relies on an accepted dependency; relies on the single-writer assumption.

**"Converted"** means a verdict of "Reconciled" and a final validation result of "passed", whether reconciliation was on or not; in a preview run nothing is converted ([ADR 0012][adr12], [ADR 0013][adr13]).

**Thresholds** (fixed by the tool, versioned): statement coverage of the original at least 95%, branch coverage at least 90%, both leaving out branches the solver proved infeasible; the surviving-mutant threshold is calibrated in the build ([Acceptance Criteria](#acceptance-criteria)).

**Independence rules.**

1. No LLM takes part in any verdict. Verdicts are computed by deterministic code from observations.
2. SQL Server execution of the original is the only oracle. Tolerances come only from the silent-differences catalogue, and only where the original's lineage passes a catalogued difference.
3. The data generator, and the LLM step proposing inputs, see nothing derived from converted code ([ADR 0005][adr5], amended three times). The step's inputs are the original, the catalog slice, coverage gaps of the original, the domain profiles, the business-rules file and the input format. Inputs derived from converted code (candidate disagreements, mutant-directed inputs, minimised failing inputs, reviewer-written inputs) join the visible set only; the blind generator alone owns the hold-out seeds that decide a verdict.
4. Final verdicts run on hold-out seeds repair never saw. Hold-out seeds are single-use: a failure on one is disclosed to repair in full, those seeds are retired into the visible set, and fresh ones are drawn.
5. Comparison code shares no code with the support library.
6. Repair may change only the artefact's (or its unit's) representation and generated code.
7. After any repair, checks restart from layer 1.
8. Expected observations from the original are captured once per input and stored in the run catalog, so no repair attempt can shift them.
9. Every verdict records a fingerprint of the test definition: generator version, seed counts (the seed values are recorded with the verdict, not in the fingerprint), the hash of the engagement's lesson-added test inputs, comparison rules, tolerance catalogue, mutation operators, thresholds, domain-profile versions, the business-rules file's hash, the invariants, the edge-case pool version, tie-free seed counts and the solver version. A verdict whose fingerprint doesn't match the tool's pinned definition is invalid, and the retroactive search picks it up.
10. Nobody can change a verdict ([ADR 0006][adr6]). An operator may attach a recorded acceptance, with name and reason, shown beside the verdict and never in place of it.
11. The testing module is itself tested ([Testing the tool itself](#testing-the-tool-itself)).

**Cost.** The owner accepts the run-time cost of all six layers. The report states run time per layer.

### Reconciliation

Decided in: [How does reconciliation generate data and compare results?][t13], [Which SQL Server versions does v1 support?][t26], [Can a SQL Server container and local PySpark stand in for the real platforms?][t4]. ADRs: [0005][adr5], [0014][adr14], [0015][adr15].

**Reconciliation never fakes what it cannot run** ([ADR 0014][adr14]). External objects outside the extraction are never stubbed: an artefact touching one is "Not reconcilable locally" until it is extracted, and the report lists each external database whose extraction would make artefacts reconcilable, with how many it would unlock. The clock is never faked: SQL Server on Linux ignores `libfaketime` (tested). Code the sandbox can't run goes to an optional **remote tier**, a real platform instance provided for the engagement that receives synthetic data only (a Databricks workspace; on paper, a Snowflake account; or a guarded source-side test server). Every remote tier is guarded: a target-side tier is reached as a tester principal that can reach only the tool's scratch catalog, in a workspace different from the client's declared target workspace, and the tool refuses to run otherwise ([ADR 0014][adr14], amended).

**Data generation** (the generator is blind: it sees nothing derived from converted code).

- **Which tables:** the read and write sets of the artefact, its callees and the triggers involved, closed over foreign keys. Computed and identity columns are left to the engine.
- **Constraints:** types by construction; distinct values for keys; existing parents for foreign keys (orphans only where the catalog marks a key disabled or not trusted); check constraints solved where the solver can, otherwise rejection sampling within a budget (an unsatisfiable one is recorded and its table gets fewer rows). For every constraint on a table the artefact writes, inputs are also generated that make its writes violate it, exercising the write wrapper and the original's error paths.
- **Values:** discriminating values (non-zero, unequal, varied magnitude) and an edge-case pool per type and collation drawn from the silent-differences catalogue: `NULL`, empty string, trailing spaces, mixed case, Unicode, boundary dates, numeric boundaries and values near overflow, values that round, duplicates in non-key columns. Tables hold 50 rows by default; faults that show only at volume are not tested, and the report says so.
- **Row placement on Spark:** several partitions with shuffled row placement, fixed per seed, so code depending on row order fails in the sandbox.
- **Inputs solved from the original's conditions:** every `WHERE`, `IF`, `CASE WHEN` and join condition is encoded for Z3, modelling T-SQL semantics (`NULL` three-valued logic, collation, implicit conversion), to find inputs making each true and false. What it can't model is "not solved". Solved targets feed visible and hold-out seeds. The same mechanism aims inputs at a lesson pattern's region.
- **LLM proposals** use the same input format, are checked against every constraint, and join the visible set only. The cost: a branch only a proposal reaches never counts toward the deciding run.
- **Seeds:** 5 visible and 3 hold-out per verification by default, both versioned.
- **Domain profiles and the business-rules file.** Declarative, in a fixed vocabulary (value domains, distributions, relationships between columns, invariants: sum-equals, count-equals, unique-where, not-null-where, references). A deterministic mapping by name and type binds them to tables and columns automatically; the report lists every mapping and unused rule, and the operator may override. Most rows follow the rules; a fixed share deliberately breaks them where the database's own constraints allow. v1 ships four profiles: retail sales and orders, inventory, general ledger and finance, people and HR. A new industry profile ships in a release once a corpus from that industry exists to test it against. The business-rules file is engagement input in the control database, read only by the testing module and the test-input step.
- **The input format:** one typed document per input with parameters by name, rows per table, DML for triggers and the source position aimed at, values in a canonical text form per neutral type, so one document loads on any source and target. The comparison never reads it.
- **Coverage gaps** each have a position, a kind (statement, or a side of a branch), the condition's text and a status: not reached; reached on visible seeds only; not solved (naming the construct); unknown (some construct on the path not fully modelled); infeasible (proved unreachable, with every construct on the path fully modelled). All but infeasible count against coverage; infeasible ones are listed.

**Running both sides.**

- **Isolation:** each verification gets its own scratch database on the sandbox SQL Server, built from catalog definitions with the client's database collation, compatibility level and per-routine settings, with the originals of the artefact, its callees and its triggers. A database snapshot is taken after loading and reverted before each input. On Spark, each verification has its own session and warehouse directory, with Delta tables reloaded per input. Both are dropped afterwards. The cost: a few seconds per input on Spark, so a run over thousands of artefacts takes hours.
- **Invoking each artefact type:** a procedure by `EXEC` with every result set, output parameters, return value, messages, error and after-state captured; a scalar function over all argument tuples in one query; a table-valued function and a view by `SELECT *`; a trigger through generated DML on its table (single-row, multi-row and zero-row inserts, updates and deletes); a batch as written. The converted side is called the same way through its entry point.
- **The clock:** the SQL Server side records the time before and after each call and the converted side's clock is set to the start; a time-derived value (found from the original's data flow) matches if it falls inside that window, and every use of the window is reported under timeliness. Session values (`SUSER_SNAME()`, `HOST_NAME()`, `DB_NAME()`, `@@SPID`) are read from SQL Server and given to the converted side.
- **The sandbox per run:** one SQL Server container per run, matched to the client in server collation (which temp tables use), version (by the routing in [section 16](#16-sql-server-versions)) and time zone; the Spark container for the client's declared Databricks Runtime ([section 7](#7-the-support-library)); all versions recorded in every verdict. A mismatch the tool can't fix carries "assumed" provenance. The final validator's fresh sandbox is a separate container.
- **The client:** the runner uses SqlClient with errors delivered as messages, and cross-checks it against Extended Events' per-statement row counts.

**Comparing.**

- **Rows** are compared as multisets; under a top-level `ORDER BY` the order is checked on the sort keys only, ties in any order. Column names exactly; types through the type mapping.
- **Values:** decimals and money exact at the mapped scale; strings exact, case and trailing spaces included; dates and times exact at the source type's precision; floats exact, except that silent-difference entry 3.13 gives a tolerance for columns whose lineage includes a floating-point aggregation, named in the evidence. One more catalogued tolerance: 100 ns for the seventh fractional digit of `datetime2(7)`, `time(7)` and `datetimeoffset(7)` columns (entry 4.3), reported per artefact. Apart from these two, no tolerance applies to a whole type or column. Lineage is the original's data flow, found by the parser.
- **Exclusions,** each listed in the verdict: `NEWID()` and `RAND()` outputs and everything derived from them; for `TOP` without `ORDER BY`, the count must match and every converted row must belong to the full qualifying set (from a parser-made variant run on SQL Server as a check, never as the oracle); for case-insensitive `GROUP BY` and `DISTINCT`, a value matches if case-insensitively equal and that spelling occurs in the group's input. Identity values are compared, because the wrapper reproduces them. Row versions are compared by property: every written row gets a new, strictly larger value, unchanged rows keep theirs.
- **Plan-dependent constructs** are checked by property, and each occurrence is reported as a pre-existing issue in the legacy logic: ranking over keys that aren't unique must be a valid ranking (consistent with the order keys, ties in any order); `SELECT @v = @v + col` must combine exactly the qualifying values in some order. Outputs derived from them: a filter on the rank keeps only rows SQL Server could have kept under some valid order of ties; an accumulated value passed on as-is must equal some ordering of the qualifying values; any other downstream use is compared by count, type and nullness only on seeds with ties, listed as "not compared by value". **Tie-free seeds** (unique order keys; a single qualifying row or identical qualifying values), derived from the original's structure, feed visible and hold-out seeds, and on them every output is compared by value.
- **Errors and messages:** system errors match on number and severity (text compared and reported, not deciding); errors the code raises itself (`THROW`, `RAISERROR` above severity 10) match on number, state and exact text; messages are compared and reported, not deciding. After an error the original continued past, where ADR 0004 makes converted code stop, only "an error was raised" is compared, listed as a caveat.
- **Data-quality dimensions.** Every difference is classed by fixed rules, and a failure leads with the first dimension hit in this order:

  | Difference | Dimension |
  |---|---|
  | Count, missing or extra rows, a `NULL` in the wrong place | Completeness |
  | Duplicates | Uniqueness |
  | Type, length, constraint or error | Validity |
  | Value | Accuracy |
  | Foreign-key break in an after-state | Consistency |
  | Equal once normalised, different as written (padding, case, date style, GUID text) | Conformity |
  | Clock-derived | Timeliness |

- **Business invariants** run on both sides' after-states, by one implementation in the comparison code, and only on inputs that follow every rule:

  | Holds on the original | Holds on the conversion | Reconciliation | Outcome |
  |---|---|---|---|
  | Yes | Yes | Any | Nothing extra |
  | No | Any | Any | Pre-existing issue, flagged beside an unchanged verdict |
  | Yes | No | Passed | Blocked by a testing-module defect |
  | Yes | No | Failed | Failed, as found |

- **Minimising a failing input:** delta debugging over rows and parameter values within a budget, re-running both sides. The minimised input joins the visible set only and is the evidence repair sees.

### The inner checks

Decided in: [How do the inner checks work: statement tracing, expression checks and mutation testing?][t24], [Which autonomy level does each component get?][t14]. Facts were tested on SQL Server 2022 CU27 and the sandbox's Python 3.12.

**Statement tracing.**

- **Coverage of the original** comes from Extended Events (`sp_statement_completed`) on the **uninstrumented** original, filtered to the verification's session and scratch database. It reports every statement executed in procedures, triggers and non-inlined functions, with offsets and row counts, and `IF` conditions as statements. A scalar function SQL Server 2019+ inlines produces no events: its body is covered in a separate run with inlining off, never the oracle, and the report says so.
- **The instrumented copy** is made by the parser, never altering the original. Each checkpoint leaves SQL Server as user events (`sp_trace_generateevent`), which survive `ROLLBACK` and `XACT_ABORT` aborts; state is serialised with `SELECT … FOR JSON` in chunks of up to 8,000 bytes, and `@@ROWCOUNT` restored afterwards with `SELECT TOP (@saved)`. A user event leaves `@@ROWCOUNT`, `@@IDENTITY`, `SCOPE_IDENTITY()` and `@@TRANCOUNT` unchanged but resets `@@ERROR`, so where the next statement reads `@@ERROR` the checkpoint moves after that read.
- **Placement:** after every statement; once per loop iteration; in callees and triggers too, with their call path; at the boundaries only of a raised region.
- **A checkpoint holds** every variable in scope with its type; the full contents of temp tables, table variables and permanent tables written so far; `@@ROWCOUNT` and `@@TRANCOUNT`; each open cursor's status; inside a `CATCH`, the error state. Reconciliation's exclusions apply.
- **The converted side** is traced with Python's `sys.monitoring` (Python 3.12; every Databricks Runtime from 16.4 LTS to 19 uses 3.12.3), with no markers in delivered code: at each line that ends a linked statement it reads session state and locals and collects DataFrames. The fallback, should a runtime lack it, is `sys.settrace`.
- **Equivalence guard:** on every input, the instrumented copy must give the same observation as the original, and a traced converted run the same as an untraced one; otherwise tracing is unavailable for that artefact, the verdict says so, and the end-to-end comparison still stands.
- **Matching:** a checkpoint is identified by source position, call path and occurrence count; pairs are compared under reconciliation's rules; the first divergence is repair's evidence, and a checkpoint on one side only means control flow diverged there.

**Expression checks.** Every scalar expression of the original with a linked counterpart, predicates included, checked from the innermost outward. On SQL Server, inputs go into a table with the declared types and collations and `SELECT <expression> FROM inputs` runs under the original's settings; on Spark, the linked span is cut from the delivered code and evaluated over a DataFrame of the same inputs. Row-level expressions get tuples; aggregates get generated groups (empty, one row, all `NULL`, large); window functions get partitions with ties. Expressions with a subquery or table reference are covered end to end and listed. Inputs: every combination of edge-pool values for up to 2 inputs, all-pairs for more, solver-made inputs per `CASE` arm and predicate side, and 200 random tuples per visible seed. Visible seeds only.

**The same-engine check** is a target-owned check of the generator at generation time: it renders Spark SQL from the representation and runs it against the PySpark output. On disagreement, the generator defect is flagged, the inputs join that expression's visible inputs, and SQL Server decides which rendering is wrong; a wrong PySpark rendering fails the artefact. The cost: a second renderer, built and maintained only for this check.

**Mutation testing,** on the generated Python:

| Group | Mutations |
|---|---|
| Comparisons, arithmetic, logic | Swap operators; swap `AND` and `OR`; negate a condition |
| Constants | Add or subtract one |
| Filters, columns | Drop a filter predicate; swap a column for another of the same type in scope |
| Nulls | Remove `NULL` handling; swap null-safe and plain equality |
| Joins, aggregates, ordering | Swap the join type; swap an aggregate, add or drop `DISTINCT`; drop a sort key, swap direction |
| Silent differences | Replace each helper handling a catalogued difference with its naive built-in, from the API manifest |
| Statements, exceptions, transactions | Drop a write, assignment or call; drop an `except` branch, turn a re-raise into a swallow; move a commit, skip a rollback |

Each mutant runs on visible seeds and stops at its first kill; survivors run on hold-out seeds too; a mutant at a site no input reached is not run and counts as a survivor ("not covered"). A mutant is equivalent only when shown deterministically: an optimised Spark logical plan identical to the unmutated code's, or a solver proof for a fully modelled scalar expression. No model or person marks a mutant equivalent. Survivors trigger up to 5 extra blind visible seeds and, where the solver can encode the difference, mutant-directed inputs (visible only). The survival rate left is compared with the threshold; above it, the verdict is "Reconciled, weakly tested". The cost: thousands of mutants for large procedures, accepted.

**Candidates.** For lowering assist and hard raising: each candidate passes layers 1–4 on visible seeds; those that pass run against each other on Spark over the generator's inputs; disagreeing inputs join the visible set and layer 4 runs again; a fixed, versioned rule picks one of those still standing (for example fewest helper calls, then shortest code); only the chosen candidate goes on to hold-out seeds and mutation testing. The cost: up to three times layers 1–4 for those steps.

### The final validator

Decided in: [How does the final independent validator work?][t32], [Observe the ODBC driver after a failed statement][t34]. ADR: [0013][adr13].

**A second, independent judge.** An artefact counts as converted only if its verdict is "Reconciled" and it passes final validation. The validator's result is a separate, unchangeable record (passed, failed, not validated) beside the verdict, which is never changed.

- **Inputs, and only these:** the original source from the snapshot; the delivered project exactly as shipped; the snapshot's raw capture, from which it rebuilds the original's database with its own code, never the catalog's parsed facts. It never sees the representation, source links, the run plan, the testing module's verdicts, seeds or data, lessons, worked examples, repair history or intent summaries, and never reads the support library's API manifest or the silent-differences catalogue.
- **A fresh sandbox,** running the delivered project from its own packaging, so packaging and environment faults are caught. It follows the same version routing, and needs its own path on any remote tier or old-version test server, with its own login and scratch databases.
- **Shares no code with the testing module.** Its own generator: coverage-guided random generation from the raw definitions, with a boundary list of its own, feedback from coverage read through Extended Events by its own code, tables from empty up to 5,000 rows, and a fixed input budget. Its own observations: after every call it snapshots every table in its database, with result sets, output parameters, return value, errors and messages. Its own comparison: exact on every value through the published representation rules, with one floating-point bound for aggregates set by its own authors. Its own client: mssql-jdbc, reading on after each error, with jTDS as its fallback for old test servers, cross-checked against Extended Events' row counts. Only the shipped support library is shared, because delivered code imports it; it is part of what is judged. In the build it is written by an agent that has not seen the testing module's code.
- **Without the shared parser:** nondeterminism is found by running each input on the original three times, varying database-scoped `MAXDOP` and the physical load order; output that differs is compared only by row count, types and nullness. The cost: an unordered `TOP` that happens to agree three times can make it fail a correct conversion, which then waits for a release; the defect notice names the output. Time-derived output is found by observation, through its own execution window. A case-insensitive value matches if equal under the column's collation and that spelling occurs in its own input.
- **Coverage** of the original comes from Extended Events through its own code, drives its generator, and is reported beside the testing module's, never merged.
- **Deterministic:** SQL Server execution is the only judge, and no model decides anything.
- **One-way, apart from a pass-or-fail bit.** Only the pass or fail result may be used, by five consumers: lesson confirmation, worked-example qualification, the pilot barrier, dependency blocking, and the orchestrator's observation ([ADR 0013][adr13], amended). Its inputs, evidence and failure details never reach any conversion component.
- **A failure** marks the artefact "Failed final validation", not converted, and flags a defect in the testing module. The failing input is treated like a disclosed hold-out seed: maintainers may see it, it never enters the testing module's data, and it is retired. A testing-module fix counts only if the testing module then catches the failure with its own blind data; repair acts on the artefact only after that. The defect notice says when the testing module had passed it under a tolerance. One deterministic step needs no release: an artefact delivered in a raised form falls back to its faithful form and is validated again with fresh seeds, after which one short final step re-verifies and re-validates it and its dependents, once per artefact. If the faithful form fails too, it is not converted and its dependents are blocked.
- **"Not validated"** whenever it cannot recreate an object or run something, with the reason. Never a pass.
- **Callees and cycles:** each artefact is validated against the delivered callees and triggers, and a cycle as one unit.
- **Storage:** its seeds are single-use, and they and all its evidence live in its own schema of the control database under its own role, which the engine's role cannot read.
- **One capability, "validate a subset as a delivered project"** (the subset with its callees and triggers, packaged, in a fresh sandbox), used three times in a run: after the pilot, before the bulk; on demand, to confirm a lesson drafted during the bulk, on that lesson's own artefacts; and for the whole project at the end. Each use draws fresh seeds.
- **Re-runs:** a validation result is kept against a key: the hashes of the artefact's delivered code, its callees and triggers, and the raw definitions it read; the validator version; the support library version; the SQL Server and Spark image versions. A re-run validates afresh only artefacts whose key changed, plus an **audit sample** of unchanged artefacts (5% by default, versioned, its seed recorded). The report gives each validation's date and seed count, and an operator may force a full re-validation. The cost: an unchanged artefact outside the audit sample keeps a pass earned on earlier seeds.
- **Preview runs:** no validation, only a packaging check (install the project in a fresh sandbox, import every module, call each entry point on empty tables), reported and counting for nothing.

### Testing the tool itself

Decided in: [What does the testing module own, and how is it kept independent?][t20], [How do the inner checks work: statement tracing, expression checks and mutation testing?][t24], [How is the support library packaged, versioned and tested?][t22], [How does the final independent validator work?][t32], [How does the tool learn from its mistakes, and how are earlier conversions brought up to date?][t23].

- **The testing module's self-test** runs on every new version of the testing module, the data generator or a generator. Every **known-bad case** must fail at some layer, and the layer that caught it is recorded; every **known-good** case must pass. For each entry in the silent-differences catalogue, a conversion that ignores it is a known-bad case. Known-bad cases also come from lessons. Known-good cases come from reconciled conversions, the hand conversions once reconciled, never the release hold-out. A version that lets a known-bad case through, or fails a known-good one, may not issue verdicts.
- **Each judge's client cross-check** is confirmed on known cases, including nested statements such as triggers, before the judge may issue results.
- **The support library's tests.** Each helper has a spec naming the T-SQL construct it reproduces and the catalogue entries it handles. Its tests run the construct on SQL Server and the helper on Spark over the same inputs (edge-case pools, random values, every entry it names), across a matrix of collations (case- and accent-sensitive and insensitive, `SQL_` and Windows collations, `_BIN2`, `_100_`, UTF-8), every compatibility level at which its behaviour differs, each supported SQL Server container version, and several partitions with shuffled row placement. The tests' comparison code shares no code with the library. Each library version declares the Databricks Runtimes it supports and is tested on local Spark pinned to each, with the Python patch version pinned too. Each release also runs the suite once on the vendor's own Databricks workspace per supported runtime, with synthetic data only, to catch what local Spark can't reproduce (Photon, Databricks' own patches). Each twin and its column helper are tested against the same SQL Server cases, and a property test on every release checks they agree on random inputs.
- **The validator's self-test,** before a version may issue results: it must kill its own fixed sample of mutants of known-good delivered projects (using neither the testing module's known-bad cases nor its mutation operators); known-good delivered projects must pass (corpus artefacts the testing module reconciled, each signed off by a named maintainer, and once validator versions exist, passed by an earlier one); and **identity runs** must pass, running each self-test artefact with the original on both sides, SQL Server against SQL Server, through its own generator, nondeterminism detection, clock window and comparison. The self-test records which checks rest on identity runs and which on reviewed projects; identity runs never touch the Spark side or the representation rules.
- **Write leases and recovery on start** are exercised by both judges, and crash-recovery cases are part of the support library's tests.
- **Prompts and models** change only in a release gated by the known-good and known-bad suites and by each AI step's measured pass rate.

### Prior art in this repository

- **The sandbox:** [`sandbox/`](../../sandbox/), a SQL Server 2022 CU27 container and a toolbox container (Python 3.12, PySpark 4.0.0, Delta 4.0.0, JDK 17, Microsoft's ODBC Driver 18 and `pyodbc` as the test record), pinned to Databricks Runtime 17.3 LTS.
- **The corpus:** [`corpus/`](../../corpus/), rebuilt by `python corpus/build.py`, with [`corpus/manifest.json`](../../corpus/manifest.json) listing 133 artefacts: a 35-artefact sample corpus (`RetailSample`, written to exercise every converted type and the silent differences) and a 98-artefact public corpus (AdventureWorks2022 and WideWorldImporters). 69 stored procedures, 8 to 2,413 lines. The manifest's construct tags come from pattern matching, not a parser.
- **The inferred-difference tests:** [`corpus/differences/`](../../corpus/differences/), 13 of 14 inferred differences confirmed on SQL Server and local Spark, 1 refuted.
- **The hand conversion:** [`prototype/hand-conversion/`](../../prototype/hand-conversion/), nine artefacts converted by hand with a classified step log and the support-library calls they needed. Unverified: nothing was run.
- **The report prototype:** [`prototype/report/`](../../prototype/report/), the winning layouts (sign-off variant A, engineering variant B) rendered from one data file, with PDFs byte-identical across renders.
- **The vocabulary check:** `corpus/tools/vocabulary_check.py`.
- **Driver probes:** [`corpus/tools/odbc_errors.py`](../../corpus/tools/odbc_errors.py), [`corpus/tools/sqlclient_errors/`](../../corpus/tools/sqlclient_errors/) and [`corpus/tools/jdbc_errors/`](../../corpus/tools/jdbc_errors/), comparing clients after a failed statement.

## Acceptance Criteria

Decided in: [What acceptance criteria make the five selling points checkable?][t19], with the thresholds it set for [What does the testing module own, and how is it kept independent?][t20].

**Every criterion is a release gate.** Each is measured on the sample and public corpora with reconciliation on, over three cold runs with the cache cleared, and the worst run counts. A release that misses any gate doesn't ship; the measurement report names each missed criterion, and the next passing release's notes record it. No partial pass, and no waiver except a recorded change to the criterion itself. Thresholds change only by a release. The first real client corpus re-measures every criterion in the build.

**"Converted"** means "Reconciled" and passed final validation.

**The release hold-out.** About a third of the public corpus, chosen once and deterministically (stratified by artefact type, selected by a hash of the name, recorded in the manifest), enlarged only from new public or real corpora.

- It carries the headline numbers, reported separately; sample-corpus and public-corpus results are also reported separately.
- It is never used for development: not for rules, prompts, lessons, the global worked-example pool, the known-good suite the lesson gate re-runs, the validator's self-test projects, the surviving-mutant calibration, model-tier calibration, or developing the orchestrators.
- **In measurement runs,** lessons drafted from hold-out artefacts are discarded, hold-out artefacts are kept out of the engagement's worked-example pool, and the pilot is drawn from development artefacts only.
- Looking at a hold-out failure counts as development: per-artefact hold-out results go only to release reviewers, and a failure seen there is fixed only once reproduced on a development artefact.
- It is small: about 33 artefacts, roughly 13 of them testable stored procedures, so one procedure is about 7.7%. Per-construct gates are therefore measured on the whole corpus and headline gates on the hold-out. **Measured on the whole corpus, the per-construct gates partly measure fit,** until a real corpus enlarges the hold-out.

**The corpus these are measured on** (`corpus/manifest.json`): 131 artefacts of converted types (69 stored procedures, 27 views, 18 functions, 13 triggers, 4 batches); 15 stored procedures contain dynamic SQL, leaving 54 testable; procedure length median 34 lines, 90th percentile 146, longest 2,413.

**Selling point 1: evidence.**

| Criterion | Target |
|---|---|
| Every artefact and every construct has a status from the vocabulary | 100% |
| Every injected degradation (fallback, trimmed context, assumption, skipped check) appears in the report | 100% |
| Report data, HTML and PDF re-render byte-identically | 100% |
| No testing-module defect open at release; no false failures in the validator's identity runs | 0 |
| Reader test before first client use: five non-technical readers each answer five questions from a sign-off document | Each at least 4 of 5 |

**Selling point 2: the hard artefacts.**

| Criterion | Target |
|---|---|
| Testable stored procedures converted | ≥ 80% |
| Artefacts of converted types converted, excluding the 15 with dynamic SQL (116) | ≥ 75% |
| Triggers converted | ≥ 75% |
| For each hard construct used by 10 or more artefacts, those artefacts converted (dynamic SQL excluded) | ≥ 70% each, on the whole corpus |
| For each hard construct used by fewer than 10 artefacts (savepoints, `MERGE`, `OUTPUT` and so on) | At least one converted |
| Artefacts not converted because of their length | 0 |

80% is an estimate, not a measurement: nothing has been converted yet. Changing it is a decision recorded in the spec, never a quiet adjustment.

**The "weakly tested" thresholds,** per artefact, after the extra blind seeds and mutant-directed inputs:

| Measure | Definition | Threshold |
|---|---|---|
| Surviving mutants | Mutants nothing reaches, and any not shown equivalent deterministically, count as survivors | Calibrated in the build on known-good conversions (reconciled, reviewed hand conversions), recorded with its measurement and versioned; 5% is the starting target |
| Statement coverage of the original | Infeasible branches, proved by the solver, left out | ≥ 95% |
| Branch coverage of the original | Infeasible branches left out | ≥ 90% |

**Selling point 3: silent differences handled.**

| Criterion | Target |
|---|---|
| Every catalogue entry has a known-bad case the testing module catches | 100% |
| Every support-library helper passes its whole SQL Server matrix | 100% |
| Mutants swapping a silent-difference helper for its naive built-in, at reached sites, are killed | 100% |

**Selling point 4: repeatable output.**

| Criterion | Target |
|---|---|
| A warm re-run produces a byte-identical delivered project, and byte-identical report data outside its run-specific section (times, durations, cost, audit-sample results) | 100% |
| Across three cold runs, artefacts with the same status in all three | ≥ 95% |
| An injected helper fix reaches every artefact that uses the helper | 100% |
| An edit to delivered code is detected by `check-project` | 100% |

**Selling point 5: target-neutral.**

| Criterion | Target |
|---|---|
| Core packages import nothing from a source or target package, by a static dependency check | 0 violations |
| The Spark SQL renderer for the same-engine check is built only against the target contract | No core change |
| The Oracle and Snowflake paper designs change no existing meaning in the vocabulary | None |

**Other release criteria.**

| Criterion | Target |
|---|---|
| A cheaper model tier serves a complexity band only if its pass rate is at least the top tier's, over three cold runs, with at least 20 steps in the band | Per band |
| Model spend per converted artefact | Reported, no threshold |
| Wall-clock time for the whole corpus | Reported, no threshold |

## Build Commitments

What this spec commits the build to, beyond implementing the decisions above. Each was decided in the ticket named.

**The build's first experiment: the orchestrator benchmark** ([What orchestrator interface lets an agent and a plain pipeline be swapped?][t15], [ADR 0020][adr20]).

- **The hypothesis** is the owner's: an agentic orchestrator converts more than a plain pipeline. It is tested, not assumed. No directional prototype was run before the build: its setup would have differed too much from the product to give trustworthy evidence.
- **Setup:** the development corpus (the sample corpus plus the public corpus minus the release hold-out, which stays untouched), split deterministically into a tuning half and a benchmark half; reconciliation on; identical settings for both orchestrators (parallelism, limits, routing, versions). The benchmark needs the whole engine built first.
- **Two conditions, 5 cold runs of each orchestrator in each,** 20 runs in all: an ample ceiling that doesn't bind, and a binding ceiling set after the ample runs at 60% of the baseline's measured spend there.
- **Deciding, in each condition,** on the benchmark half: on converted share, the agentic orchestrator wins only if its mean beats the baseline's by more than the larger of the two run-to-run ranges and by at least 2 artefacts; otherwise it's a tie, and speed decides by the same rule on mean wall-clock time. A tie on both makes the baseline the default.
- **Guardrails, flagged but not deciding:** cost per converted artefact more than 2× the baseline's; status stability below 95%.
- **Also tested:** an interrupted run resumes by replaying; a crash between doing the work and recording it duplicates no effect.
- **Development rule:** the agent's prompt and tools are developed on the tuning half only. The cost: a smaller, noisier benchmark.
- **After it:** both orchestrators stay behind one switch; the winner under the ample ceiling becomes the default, recorded in a decision record; if the binding-ceiling result disagrees, the switch's documentation recommends the other orchestrator for engagements expected to hit their ceiling.
- **A repair arm:** 5 more cold runs under the ample ceiling, with the baseline orchestrator and repair replaced by a single fixed-prompt call given the same evidence. If it fixes as many failures, repair moves down to an LLM step ([ADR 0008][adr8], [ADR 0020][adr20], amended).

**Build-phase checks and deliverables.**

| Commitment | From |
|---|---|
| Prove the sandbox and corpus build on plain Docker Engine, for example as a Linux CI job; it passes only if `python corpus/build.py` writes an identical `corpus/manifest.json` (133 artefacts, same hashes) | [ADR 0017][adr17] |
| VM tests on Windows VMs of SQL Server 2008 R2, 2012, 2014 and 2016, and 2005 and 2000 for the remote tier (the old ones on an isolated network; licensed media a prerequisite): the extractor's grants and self-check, the raw capture, client connectivity and each client's boundary (SqlClient, mssql-jdbc, FreeTDS, jTDS, and whether each fallback returns a complete observation), compatibility-level routing, and the Windows-only feature list | [Which SQL Server versions does v1 support?][t26] |
| The VM tests start with Annex E's priority list: RC4 at level 100, `HASHBYTES` over 8,000 bytes, index keys of 901–1,700 bytes, `STEnvelope` of an empty geometry, `xml.exist` on `NULL`, and the level-130 `datetime` conversions on a 2014 engine | [Which behaviours differ between SQL Server 2008–2016 and a newer engine at the same compatibility level?][t33] |
| A legal review of the sign-off document's signature-block template before first client use | [What does the conversion report look like?][t9] |
| A check that each report's HTML and PDF stay identical in content and re-render byte-identically | [What does the conversion report look like?][t9] |
| The measurement script for every acceptance criterion | [What acceptance criteria make the five selling points checkable?][t19] |
| The deterministic, stratified selection of the release hold-out, recorded in the manifest | [What acceptance criteria make the five selling points checkable?][t19] |
| A fault-injection harness for degradations and helper fixes | [What acceptance criteria make the five selling points checkable?][t19] |
| The static dependency check (core imports nothing from a source or target package) | [What acceptance criteria make the five selling points checkable?][t19] |
| The reader test of the sign-off document | [What acceptance criteria make the five selling points checkable?][t19] |
| Calibrating the surviving-mutant threshold on known-good conversions | [What acceptance criteria make the five selling points checkable?][t19] |
| Reconciling the nine hand conversions, so they can join the known-good suite and the worked-example pool | [Which conversion steps need judgement? Hand-convert a corpus sample][t12] |
| The final validator written by an agent that has not seen the testing module's code | [How does the final independent validator work?][t32] |
| Model-tier calibration per complexity band, outside the release hold-out | [How does a run govern its model use?][t30] |
| The first real client corpus re-measures every acceptance criterion | [What acceptance criteria make the five selling points checkable?][t19] |

**Design work and values the build must set.** Each was left open on purpose; the right-hand column says what decides it.

| Commitment | What decides it |
|---|---|
| The list of neutral error classes, the PySpark half (Spark and Delta failures to classes) and the SQL Server half (classes to error numbers, messages and severities, and whether each ends the statement or aborts the batch) | The errors the corpus raises, catches or tests for (`THROW`, `RAISERROR`, `ERROR_NUMBER()` and `@@ERROR` comparisons), and the failures Spark and Delta raise for the same operations in the sandbox. Each entry is tested in the sandbox; anything unmapped gets the reserved number and is flagged ([ADR 0019][adr19], [section 6](#6-converted-code)) |
| The control database's tables, the run catalog's record format, and the snapshot export's form | What the spec requires them to hold and do: immutable snapshots with content hashes and provenance ([section 3](#3-extraction-and-the-catalog)); every record listed in [section 12](#12-the-run-catalog-caching-and-resuming), with replay and every cache key it names; the final validator's separate schema and role ([section 14](#14-the-control-database)); dropping one database to delete a client's data ([ADR 0007][adr7]) |
| Attempt limits per AI step, and the budget per artefact | Measured pass rates per step and tier on the development corpus, before the benchmark; the same values for both orchestrators |
| The token size budget per AI step | The measured size of assembled contexts on the corpus, so that the never-trimmed parts fit for the 2,413-line procedure's largest regions |
| The tier table and the complexity score's formula | Tier calibration per complexity band on the development corpus ([Acceptance Criteria](#acceptance-criteria)); the first release routes everything to the top tier |
| The default parallelism | The sandbox's capacity: memory and CPU for concurrent scratch databases and Spark sessions on the machine that runs the sandbox |
| The rejection-sampling budget, the delta-debugging budget, and the share of rows that deliberately break domain rules | Run time per layer on the corpus, against the coverage thresholds the acceptance criteria set |
| The final validator's input budget and its floating-point bound for aggregates | The validator's own authors, from its self-test (identity runs and its known-good projects), never from the testing module's values |
| The surviving-mutant threshold | Calibration on known-good conversions (already listed above) |

## Out of Scope

From [the map][map]:

- Moving data or migrating schemas.
- Building any source or target other than SQL Server → Databricks. Oracle PL/SQL and Snowflake SQL are paper designs only.
- Converting the recognised artefact types (SSIS packages, SQL Agent jobs, dynamic SQL): a later phase.
- The full orchestrator benchmark and its verdict, which are the build's first experiment ([Build Commitments](#build-commitments)).
- Using, bundling, copying from or benchmarking against Databricks' conversion tooling (the agentic code converter, Lakebridge, Genie Code). Databricks as the target platform is in scope.
- Hosting, sign-in, licensing and billing for client self-service. The engine is designed so that self-service and a graphical front end can be added later.
- Shadow runs on real client data in v1. They would need a per-client data-handling agreement and must never go through the extractor.
- Obtaining and onboarding a real client corpus: build-phase work once an engagement provides one. The acceptance-criteria thresholds are set on the sample and public corpora; the first real client corpus re-validates them during the build.
- Profiling the client's real data to shape synthetic data. The extractor never reads a row.
- SQL Server versions before 2000, Azure SQL Database and Azure SQL Managed Instance, in v1. Each would be a later source variant.
- Fable and Mythos models, the Batch API, the Files API, code execution and server tools, in v1 ([ADR 0016][adr16]). Adding any of them is a new decision.

## Further Notes

### What is still open

Three items found while compiling this spec need design work in the build, not a decision now. Each is listed under [Build Commitments](#build-commitments) with what decides it, so none is left silently.

1. **The error classes and error-mapping tables aren't written.** [ADR 0019][adr19] fixes their structure: targets map failures to classes, sources map classes to codes. The list of classes and both tables' contents exist only as examples ([What must a new source or target provide?][t17], [How is each construct with no PySpark equivalent converted?][t10]).
2. **The control database's tables and the run catalog's record format aren't designed,** nor the snapshot export's exact form ([What does the catalog hold and how does conversion read it?][t8]).
3. **Values left as versioned settings, with no number decided:**
   - attempt limits per AI step, and the budget per artefact;
   - the token size budget per AI step;
   - the tier table (the one given is an example) and the complexity score's formula;
   - the default parallelism;
   - the rejection-sampling budget, the delta-debugging budget, and the share of rows that deliberately break domain rules;
   - the final validator's input budget and its floating-point bound for aggregates;
   - the surviving-mutant threshold, calibrated in the build.

   Each is set by measurement where the acceptance criteria require it.

### Decided after compilation

The first version of this spec (commit 2e03553) listed 15 items here. An independent review on Claude Fable then found 24 more. The owner decided all of them on 18 October 2026; the decisions are recorded in [Independent review of the assembled spec][t28], and applied in this spec, the ADRs, the glossary and the map.

**The original Further Notes:**

| Item | Outcome |
|---|---|
| 1. A target-side remote tier had no guard | Guarded like a source-side one ([ADR 0014][adr14], amended); see Reconciliation |
| 2. A refusal fallback could break zero retention | Fallbacks restricted to allowed models and checked first ([ADR 0016][adr16], amended) |
| 3. The release hold-out in measurement runs | Lessons from hold-out artefacts discarded, hold-out kept out of the engagement example pool, pilot from development artefacts only |
| 4. The 75% gate's denominator | Excludes the 15 dynamic-SQL artefacts: 116 |
| 5. "Trigger" collided with database triggers | Renamed **lesson pattern** in the glossary and [ADR 0010][adr10] |
| 6. The agentic orchestrator's tier | The top tier, versioned ([ADR 0020][adr20], amended) |
| 7. Which Databricks Runtime the sandbox matches | The client's declared runtime; v1 supports 17.3 LTS and 18 LTS ([section 7](#7-the-support-library)) |
| 8. What makes a cluster | Identical lowered representation with identifiers as placeholders; literals and defaults exact; comments ignored ([section 5](#5-lowering-raising-and-generation)) |
| 9. ADR 0005's wording | Amended to cover the test-input step and list its inputs |
| 10. Error classes and tables | Still open: item 1 above |
| 11. Annex C not mapped to the vocabulary | Fixed: every entry has a Vocabulary column |
| 12. `rowversion` unsupported in the vocabulary | Fixed: mapped to `binary(length, fixed)` (vocabulary draft-2) |
| 13. Control database schema | Still open: item 2 above |
| 14. Values with no number | Still open: item 3 above |
| 15. Wording drifts in earlier records | Fixed: the map's Notes and [ADR 0017][adr17]'s list corrected |

**The review's findings:**

| Finding | Outcome |
|---|---|
| R1. The single-writer assumption ignored concurrent converted jobs | Restated across every project and enforced by **write leases** ([ADR 0021][adr21]); in the sign-off |
| R2. `retry_up` against engine escalation | The engine escalates inside a step; `retry_up` re-runs the AI step behind a failing region, refused for deterministic regions ([section 11](#11-the-orchestrator-interface)) |
| R3. Repairs to deterministic output were lost on regeneration | Kept as **repair patches**, plus a proposed-rule lesson ([ADR 0008][adr8], amended) |
| R4. Crash recovery needed the run catalog in production | Automatic recovery at the start of every run from the state table; the command still ships ([ADR 0021][adr21]) |
| R5. The sign-off claimed "behaved like the original" | A flag, "differs from the original after an error", and "except where flagged" ([ADR 0012][adr12], amended) |
| R6. `datetime2(7)` and `datetimeoffset` were lossy | Offset in a companion column, listed as a target-table requirement; a catalogued 100 ns tolerance for the seventh digit |
| R7. Catalogue entries with no treatment | Helpers for 2.7, 2.11, 3.6 and 3.10, with a caveat where a helper cannot match exactly |
| R8. Client names could reach global lessons | Promotion rejects catalog identifiers; repair's hypothesis stays client-only ([ADR 0010][adr10], amended) |
| R9. When the ceiling binds | Defined; `allocate` sets maxima; the agent's calls draw on a reserved 5% ([ADR 0020][adr20], amended) |
| R10. The agent was tuned on its benchmark corpus | The development corpus is split into tuning and benchmark halves |
| R11. Missing status rows | Added: blocked in validation, awaiting final validation, fell back to the faithful form |
| R12. Consumers of the validator's bit | Five permitted consumers listed ([ADR 0013][adr13], amended) |
| R13. The cross pairings were not walked | Annex B walks Oracle to PySpark and SQL Server to Snowflake; targets declare unrepresentable types unsupported ([ADR 0001][adr1], amended) |
| R14. Statuses to sign-off groups | A fixed mapping ([section 15](#15-the-conversion-report)) |
| R15. "Checked" against "attested" | Checked on Bedrock and Vertex, attested on the direct API, with its 2-year exception stated ([ADR 0016][adr16], amended) |
| R16. Result shapes at levels 80 and 90 | Remote tier, else the administrator's export, else "could not be described" ([section 3](#3-extraction-and-the-catalog)) |
| R17. A double tie, and repair's level | A double tie makes the baseline the default; a repair arm added ([Build Commitments](#build-commitments)) |
| R18. Byte-identical re-runs against run-specific data | Run-specific fields sit in their own section, excluded from the gate |
| R19. Seeds in the fingerprint; lessons and tests | Seed counts, not values; lesson-added inputs enter as a hash; lessons never change how tests judge |
| R20. Four extractor promises in Annex B against six | Annex B now requires all six |
| R21. Material only linked to | Moved into Annex F (extractor scripts) and Annex G (agreements, open points, tested differences) |
| R22. A Windows remote tier at level 100+ was unguarded | The guard applies to every source-side remote tier ([ADR 0015][adr15], amended) |
| R23. The glossary limited the validator to the end of a run | Amended to its three uses |
| R24. 132 against 133 artefacts | Not a gap: the 133rd is the SSIS package, which isn't SQL; Annex A now says "written in SQL" |
| Entry 9.14 | Moved to Annex D only |

<!-- Link references -->

[map]: https://github.com/Bach-To-The-Future/data-migration/issues/1
[t2]: https://github.com/Bach-To-The-Future/data-migration/issues/2
[t4]: https://github.com/Bach-To-The-Future/data-migration/issues/4
[t5]: https://github.com/Bach-To-The-Future/data-migration/issues/5
[t8]: https://github.com/Bach-To-The-Future/data-migration/issues/8
[t9]: https://github.com/Bach-To-The-Future/data-migration/issues/9
[t10]: https://github.com/Bach-To-The-Future/data-migration/issues/10
[t11]: https://github.com/Bach-To-The-Future/data-migration/issues/11
[t12]: https://github.com/Bach-To-The-Future/data-migration/issues/12
[t13]: https://github.com/Bach-To-The-Future/data-migration/issues/13
[t14]: https://github.com/Bach-To-The-Future/data-migration/issues/14
[t15]: https://github.com/Bach-To-The-Future/data-migration/issues/15
[t16]: https://github.com/Bach-To-The-Future/data-migration/issues/16
[t17]: https://github.com/Bach-To-The-Future/data-migration/issues/17
[t18]: https://github.com/Bach-To-The-Future/data-migration/issues/18
[t19]: https://github.com/Bach-To-The-Future/data-migration/issues/19
[t20]: https://github.com/Bach-To-The-Future/data-migration/issues/20
[t21]: https://github.com/Bach-To-The-Future/data-migration/issues/21
[t22]: https://github.com/Bach-To-The-Future/data-migration/issues/22
[t23]: https://github.com/Bach-To-The-Future/data-migration/issues/23
[t24]: https://github.com/Bach-To-The-Future/data-migration/issues/24
[t25]: https://github.com/Bach-To-The-Future/data-migration/issues/25
[t26]: https://github.com/Bach-To-The-Future/data-migration/issues/26
[t27]: https://github.com/Bach-To-The-Future/data-migration/issues/27
[t28]: https://github.com/Bach-To-The-Future/data-migration/issues/28
[t30]: https://github.com/Bach-To-The-Future/data-migration/issues/30
[t31]: https://github.com/Bach-To-The-Future/data-migration/issues/31
[t32]: https://github.com/Bach-To-The-Future/data-migration/issues/32
[t33]: https://github.com/Bach-To-The-Future/data-migration/issues/33
[t34]: https://github.com/Bach-To-The-Future/data-migration/issues/34
[adr1]: ../adr/0001-core-owned-typed-intermediate-representation.md
[adr2]: ../adr/0002-compensating-restore-transactions-by-default.md
[adr3]: ../adr/0003-write-wrapper-enforces-source-constraints.md
[adr4]: ../adr/0004-stop-at-first-error-unless-at-at-error-is-read.md
[adr5]: ../adr/0005-test-data-generator-never-sees-converted-code.md
[adr6]: ../adr/0006-verdicts-cannot-be-overridden.md
[adr7]: ../adr/0007-postgres-control-database-per-engagement.md
[adr8]: ../adr/0008-repair-is-the-only-agent-outside-the-orchestrator.md
[adr9]: ../adr/0009-every-ai-call-starts-from-assembled-context-only.md
[adr10]: ../adr/0010-lessons-are-drafted-and-generalised-by-code.md
[adr11]: ../adr/0011-a-reviewer-may-supply-code-for-one-region.md
[adr12]: ../adr/0012-only-reconciled-counts-as-converted.md
[adr13]: ../adr/0013-an-independent-final-validator.md
[adr14]: ../adr/0014-reconciliation-never-fakes-what-it-cannot-run.md
[adr15]: ../adr/0015-old-compatibility-levels-reconcile-only-on-a-guarded-test-server.md
[adr16]: ../adr/0016-v1-can-promise-zero-retention-at-every-model-location.md
[adr17]: ../adr/0017-the-products-tools-are-free-and-open-source-where-possible.md
[adr18]: ../adr/0018-the-support-library-ships-as-source-vendored-per-project.md
[adr19]: ../adr/0019-shared-mappings-are-split-by-source-and-by-target.md
[adr20]: ../adr/0020-orchestrators-are-policies-over-a-fixed-engine.md
[adr21]: ../adr/0021-delivered-jobs-hold-a-write-lease-on-every-table-they-write.md
[vocab-json]: ../spec-drafts/operation-vocabulary.json
[r-perm]: ../research/extractor-permissions.md
[r-replay]: ../research/replayable-agent-loops.md
