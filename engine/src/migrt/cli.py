"""The command line, the v1 front end. It parses arguments, calls the engine's library
interface and prints the structured result as JSON. It holds no conversion logic.

    python -m migrt preview --engagement NAME --database DB --artefact DB.schema.name --out DIR

Connection settings come from options or the environment: the extractor's password only
from the environment variable named by --password-env, never from the command line.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import sys

from . import engine


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="migrt", description="The script migration tool.")
    sub = p.add_subparsers(dest="command", required=True)
    preview = sub.add_parser("preview", help="convert artefacts with reconciliation off; nothing is converted")
    preview.add_argument("--engagement", required=True, help="engagement name; names its control database")
    preview.add_argument("--control-dsn", default=os.environ.get("MIGRT_CONTROL_DSN"),
                         help="Postgres server for control databases (default: $MIGRT_CONTROL_DSN)")
    preview.add_argument("--server", default=os.environ.get("MSSQL_HOST", "sqlserver"), help="SQL Server host")
    preview.add_argument("--port", type=int, default=1433)
    preview.add_argument("--login", default="migration_extractor", help="the extractor's read-only login")
    preview.add_argument("--password-env", default="MIGRT_EXTRACTOR_PASSWORD",
                         help="environment variable holding the login's password")
    preview.add_argument("--database", action="append", required=True, help="a database to extract; repeatable")
    preview.add_argument("--time-zone", help="the server's time zone; needed only where CURRENT_TIMEZONE() fails")
    preview.add_argument("--artefact", action="append", required=True, help="database.schema.name; repeatable")
    preview.add_argument("--out", required=True, type=pathlib.Path, help="where the delivered project is written")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.control_dsn:
        print("migrt: no control database server: pass --control-dsn or set MIGRT_CONTROL_DSN", file=sys.stderr)
        return 2
    password = os.environ.get(args.password_env)
    if password is None:
        print(f"migrt: the login's password is not in ${args.password_env}", file=sys.stderr)
        return 2
    engagement = engine.Engagement(args.engagement, args.control_dsn)
    source = engine.SqlServerSource(args.server, args.port, args.login, password, args.database, args.time_zone)
    try:
        result = engine.preview_run(engagement, source, args.artefact, args.out)
    except (engine.ExtractorPermissionError, engine.UnsupportedServer, engine.TimeZoneRequired) as exc:
        print(str(exc), file=sys.stderr)
        return 3
    summary = {
        "run_id": result.run_id, "snapshot_id": result.snapshot_id, "preview": result.preview,
        "report_data": str(result.report_data_path),
        "artefacts": [{"identity": a.identity, "status": a.status, "file": str(a.file),
                       "layer1": a.layer1.to_json(), "manual_items": [dataclasses.asdict(m) for m in a.manual_items]}
                      for a in result.artefacts],
    }
    print(json.dumps(summary, indent=2))
    return 0 if all(a.layer1.passed for a in result.artefacts) else 1
