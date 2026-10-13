# Corpus

The artefacts the design is judged against, and the sandbox SQL Server that holds them.

## Rebuild everything

```
python corpus/build.py
```

Needs Docker (running) and Python 3.9+ on the host. Nothing else is installed on the host: the work runs in containers. The first run downloads about 330 MB of sample database backups and builds a toolbox image with PySpark, which takes a while; later runs take about a minute.

| Option | Effect |
|---|---|
| `--differences` | Also run the T-SQL versus Spark difference tests and rewrite `differences/results.*` |
| `--reset` | Remove the SQL Server data volume first, for a clean server or a new collation |
| `--skip-public` | Build only the sample database; the manifest is not rewritten |
| `--force-restore` | Restore AdventureWorks and WideWorldImporters again |
| `--skip-smoke` | Do not run the sample artefacts after building |

## What the build produces

- **A SQL Server container**, `data-migration-sandbox-sqlserver-1`, listening on `127.0.0.1:14333`. Log in as `sa` with the password in `sandbox/.env`, which the first build generates and git ignores. Image tag, edition, collation and port are set in that file; `sandbox/.env.example` documents them.
- **Three databases** in it:
  - `RetailSample`, built from `sample/`. The sample corpus.
  - `AdventureWorks2022` and `WideWorldImporters`, restored from Microsoft's backups. The public corpus.
- **A SQL Server Agent job**, `RetailSample - Nightly sales roll-up`, created in `msdb`.
- **`public/export/`**: the source of every public artefact, one file each. Not committed; rebuilt each time.
- **`manifest.json`**: every artefact in both corpora, with its type, tier, size, hash and the constructs it uses.

Stop the server with `docker compose -f sandbox/docker-compose.yml stop`. Start it again with `docker compose -f sandbox/docker-compose.yml up -d sqlserver`; the databases persist in a Docker volume.

## Layout

```
corpus/
  build.py                 the one command
  manifest.json            generated
  sample/
    database/              table shapes and seed rows for RetailSample
    artefacts/
      procedures/          one file per stored procedure, as CREATE statements
      views/  functions/  triggers/
      batches/             query and DML batches: files, not database objects
      recognised/          an SSIS package and a SQL Agent job
    exercises.json         which silent differences each artefact exercises
    smoke_test.sql         runs every sample artefact once and checks the results
  public/
    backups.json           where the public backups come from, with their hashes
  differences/
    cases.py               one test per inferred difference
    results.md             generated: confirmed or refuted, per difference
    results.json           generated: the raw observations
  tools/                   run inside the toolbox container by build.py
sandbox/
  docker-compose.yml       SQL Server and the toolbox
  tools.Dockerfile         Python, PySpark, Delta and a SQL Server driver
```

## The sample corpus

`RetailSample` is a small order-processing database written for this project. Its artefacts are meant to look like legacy code and to cover every converted artefact type, with constructs and silent T-SQL versus Spark differences spread across them on purpose. Each artefact's comments say what it relies on.

- The database collation is case-insensitive. `ref.Product.Sku` is case-sensitive, so that per-column collation matters.
- The seed rows include nulls, mixed case, trailing spaces, boundary dates, `datetime` values that round, and duplicates in staging.
- Dynamic SQL, a recognised construct, appears in `sales.uspSearchOrders` (`sp_executesql`) and `stg.uspPurgeStaging` (`EXEC (@sql)`).
- The SSIS package is hand-written XML in the package format. SSIS cannot run in a container, so it was never opened or run by SSIS. It is there to be recognised.

`smoke_test.sql` runs on a scratch copy of the database on every build, so the artefacts are known to execute and to behave as their comments claim.

## The public corpus

AdventureWorks and WideWorldImporters are Microsoft's sample databases, MIT licensed. Their backups are downloaded on first build into `public/.cache/` and checked against the hashes in `public/backups.json`. Neither the backups nor the exported source is committed; the manifest lists every artefact so they can be referred to by name.

## The manifest

`manifest.json` lists each artefact with:

- `corpus` (`sample` or `public`), `database`, `name`
- `type`: `stored_procedure`, `view`, `function`, `trigger`, `batch`, `ssis_package` or `agent_job`
- `tier`: `converted` or `recognised`
- `code_lines`: lines left after comments and blank lines are removed
- `sha256` of the source with LF line endings
- `constructs`: found by pattern matching in `tools/constructs.py`. This is an index for choosing artefacts, not a parse: it can miss a construct or report one that is not there.
- `exercises` (sample only): the silent differences the artefact was written to exercise

## The difference tests

The research in `docs/research/tsql-spark-silent-differences.md` could only infer 14 of its 82 differences. `differences/cases.py` holds one test for each. `python corpus/build.py --differences` runs every test on SQL Server and on local Spark and records confirmed or refuted in `differences/results.md`.

Local Spark here is open-source Spark 4.0.0 with Delta 4.0.0, the versions in Databricks Runtime 17.3 LTS. It is not a Databricks Runtime, so a result here is evidence about Spark, not proof about Databricks.
