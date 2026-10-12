# Script Migration

Converts the script artefacts of a legacy data platform into equivalent code for a modern data platform. It converts scripts only: it does not move data or schemas.

## Language

**Source**:
A legacy platform whose script artefacts are converted, such as SQL Server. A source is a set of artefact types, not a single SQL dialect.
_Avoid_: Source dialect, input language

**Target**:
A platform together with the language of the files produced for it, such as PySpark on Databricks.
_Avoid_: Output dialect, destination

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
The columns and types of a table, read from DDL and used only as input to conversion.
_Avoid_: Schema migration

**Core**:
The part of the tool shared by every source and every target, which no single source or target may require changing.

**Conversion report**:
The per-artefact record stating, for each construct, whether it was converted, converted with a caveat, or left as manual work.

**Flagged fragment**:
A piece of target code the report singles out for review, either because an LLM produced it or because it uses the escape hatch.

**Escape hatch**:
Target code that falls back to embedded SQL because the construct has no reasonable idiomatic equivalent in the target language.

**Reconciliation**:
Running an artefact and its conversion on the same data and comparing the results.
_Avoid_: Validation (too broad), testing
