---
status: accepted
---

# Reconciliation never fakes what it cannot run

Some artefacts can't run in the sandbox as they are. They may read another database or a linked server the extraction didn't include, or depend on the current time. Their converted code may use something only Databricks has. A tool could stub the missing objects, fake the clock, or skip the parts that won't run, and report the rest as reconciled. We decided it does none of this:

- **External objects are never stubbed.** An artefact that touches an object outside the extraction is "Not reconcilable locally" until that object is extracted. The report lists each external database whose extraction would make artefacts reconcilable, with the number of artefacts it would unlock.
- **The clock is never faked on SQL Server.** It can't be: the sandbox's SQL Server on Linux ignores `libfaketime`, which was tested on 17 October 2026. Instead, the converted side's clock is set to the start of the SQL Server execution, and a value derived from the current time must fall inside that execution's window. Every use of the window is reported under timeliness.
- **Code the sandbox can't run goes to an optional remote tier**, a real target account provided for the engagement that receives synthetic data only. Otherwise it is "Not reconcilable locally".

We chose this because a stub or a fake is a guess about the client's system. A pass resting on a guess would let the report claim "Reconciled" for behaviour nobody ran.

The full decision is in the resolution of [How does reconciliation generate data and compare results?](https://github.com/Bach-To-The-Future/data-migration/issues/13).

## Considered options

- **Stubbing external objects** with empty tables or generated data of the right shape. More artefacts would reconcile. Rejected: the stub's contents and behaviour are invented, so agreement with it proves nothing about the client's system.
- **Faking the clock with `libfaketime`.** It would have allowed exact comparison of time-derived values. Rejected because it does not work: SQL Server on Linux reads time through its own platform layer, which the library does not intercept.
- **No remote tier at all.** Simpler and cheaper. Rejected: a client who chooses Databricks' own transactions or identity columns would then never have anything counted as converted for the artefacts affected.

## Consequences

- **At clients with many cross-database or linked-server references, many artefacts stay unconverted** until the extraction is widened. The report shows how much each widening would unlock.
- **Time-derived values are compared with a tolerance**, the execution window, and the report says so every time.
- **A remote tier needs the final validator's own, independent path on that tier too** (ADR 0013). For the Snowflake paper design, a Snowflake account is the only way anything converted to Snowflake can count as converted.
- **The same rules bind the final validator.** It never stubs or fakes either.
