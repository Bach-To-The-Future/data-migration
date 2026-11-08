# Engine

The script migration tool's engine, `migrt`: the core, the SQL Server source and the PySpark target, built from the [v1 architecture spec](../docs/spec/v1-architecture-spec.md). Its library interface is [`migrt.engine`](src/migrt/engine.py); the command line ([`migrt.cli`](src/migrt/cli.py)) is a thin front end over it.

**State:** the skeleton of [issue #36](https://github.com/Bach-To-The-Future/data-migration/issues/36). One view runs end to end as a preview run: the extractor's permission self-check, an immutable snapshot in a per-engagement Postgres control database, lowering into a first cut of the intermediate representation, the checker, generation and layer-1 code checks, and `report-data.json`. Later tickets widen each layer.

## Run it

The engine runs in the sandbox toolbox, which carries .NET, ScriptDom, Microsoft.Data.SqlClient and the Python dependencies. Build the sandbox first; that also starts Postgres and grants the extractor's read-only login by Annex F's grant set:

```
python corpus/build.py
```

Then, for a preview run of one view:

```
docker compose -f sandbox/docker-compose.yml --profile tools run --rm tools \
    python -m migrt preview --engagement retail_demo --database RetailSample \
    --artefact RetailSample.sales.vOrderTotals --out out/retail_demo
```

It writes `out/retail_demo/converted/RetailSample/sales/views/vOrderTotals.py` and `out/retail_demo/report/report-data.json`, and prints the run's result as JSON. Every artefact in a preview run is "Not reconciled": nothing in it is converted.

## Layout

```
src/migrt/
  engine.py            the library interface
  cli.py               the command line
  core/                shared by every source and target; imports neither
    catalog.py         facts with provenance, snapshots, the query interface
    control_db.py      one Postgres database per engagement; snapshots immutable
    ir.py              the intermediate representation
    checker.py         verifies the representation
    vocabulary.py      the operation vocabulary (Annex A), with its JSON copy
    differences.py     the silent-differences catalogue: the one interface to both halves
    report.py          the report data
  sources/sqlserver/   connection (SqlClient), extractor, parser (ScriptDom), names, types, lowering,
                       differences (the source half, Annex C, as data)
  targets/pyspark/     generator, layer1, differences (the target half, Annex D, as data)
tests/                 run with sandbox/engine-tests.sh
```

## Checks

```
sh sandbox/engine-tests.sh            # lint (pyflakes), then types (pyright), then tests (pytest)
sh sandbox/engine-tests.sh -k layer1  # arguments go to pytest
```

All three run in the toolbox; the tests run against the sandbox SQL Server and Postgres, through the engine's library interface, the PySpark target's layer-1 contract, and the control database for the immutability claim. Imports of .NET namespaces carry a targeted `pyright: ignore`, because pythonnet provides them only at run time.

Tests that need no server, such as `tests/test_differences.py`, also run on the host for a quick loop: `cd engine && PYTHONPATH=src python -m pytest tests/test_differences.py`. The sandbox run above stays the gate.

A pre-commit hook lints staged engine files with pyflakes on the host, without Docker. Enable it once per clone:

```
git config core.hooksPath .githooks
```

Each half of the silent-differences catalogue is a JSON file in its package, checked cell by cell against Annexes C and D and the operation vocabulary by `tests/test_differences.py`. Change an annex and its half together; each half's version is the hash of its entries.

Review applies [`CODING_STANDARDS.md`](../CODING_STANDARDS.md).
