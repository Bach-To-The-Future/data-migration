"""Capture what generation produces for the whole corpus, and compare two captures byte for byte.

The standard check for a refactor that must not change output (engine/README.md): capture before, change the code,
capture after, compare. A capture holds, for each artefact of the three corpus databases, everything generation
returns for it: the generated file's text, its links and spans, the manual items, assumptions, caveats, settings,
the layer-1 result and the artefact's entry in the report data; or, for an artefact generation fails on, the error.

    capture   runs in the sandbox toolbox (it extracts the corpus from the sandbox SQL Server):
        docker compose -f sandbox/docker-compose.yml --profile tools run --no-deps --rm -T tools \\
            python tools/generation_snapshot.py capture .snapshots/before
    compare   needs only the standard library, so it runs anywhere:
        python tools/generation_snapshot.py compare .snapshots/before .snapshots/after

`compare` exits 0 if the two directories hold the same files with the same bytes, and 1 otherwise, naming every file
that differs, is missing from one side or exists only in the other. An empty capture is a failure: it means the run
captured nothing. Capture twice on the same code and compare to check the capture itself is deterministic.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATABASES = ["RetailSample", "AdventureWorks2022", "WideWorldImporters"]
INDEX = "index.json"
SHOWN_LINES = 12          # lines of a text difference shown per file


def capture(out: pathlib.Path) -> str:
    """Generate every artefact of the corpus the engine generates, and write one JSON file for each to `out`."""
    sys.path[:0] = [str(ROOT / "engine" / "src"), str(ROOT / "engine" / "tests")]
    import dataclasses

    from generation_helpers import names_for      # the recorded name rules for a snapshot, with no control database

    from migrt import engine
    from migrt.sources.sqlserver import extractor

    out.mkdir(parents=True, exist_ok=True)
    address = extractor.ServerAddress(os.environ.get("MSSQL_HOST", "sqlserver"), 1433, "migration_extractor",
                                      os.environ["MIGRT_EXTRACTOR_PASSWORD"])
    snapshot = extractor.extract(address, DATABASES)
    identities = sorted(o.identity for o in snapshot.objects if o.kind in engine.GENERATED_KINDS)
    project_kinds = engine._project_kinds(snapshot, identities)
    names = names_for(snapshot, {})
    differences = engine._differences()

    index = {}
    for identity in identities:
        try:
            built = engine._generate_artefact(snapshot, identity, differences, names, project_kinds)
            record = {"file": dataclasses.asdict(built.file), "layer1": built.layer1.to_json(),
                      "report_item": built.report_item}
            index[identity] = "generated"
        except Exception as failed:      # recorded: a refactor must change neither which artefacts fail nor why
            record = {"error": f"{type(failed).__name__}: {failed}"}
            index[identity] = "error"
        safe = identity.replace(":", "_").replace("/", "_").replace("\\", "_")
        (out / f"{safe}.json").write_text(json.dumps(record, sort_keys=True, indent=1, default=str), encoding="utf-8")
    (out / INDEX).write_text(json.dumps(index, sort_keys=True, indent=1), encoding="utf-8")
    states = {state: sum(1 for v in index.values() if v == state) for state in sorted(set(index.values()))}
    return f"{len(index)} artefacts captured to {out}: {states}"


def _files(directory: pathlib.Path) -> dict[str, pathlib.Path]:
    return {p.relative_to(directory).as_posix(): p for p in sorted(directory.rglob("*")) if p.is_file()}


def compare(a: pathlib.Path, b: pathlib.Path) -> list[str]:
    """Every difference between two captures, byte for byte; empty if there is none. Two captures that hold no files
    are a difference: nothing was captured."""
    left, right = _files(a), _files(b)
    if not left and not right:
        return [f"neither {a} nor {b} holds any file: nothing was captured"]
    differences = []
    for name in sorted(left.keys() | right.keys()):
        if name not in right:
            differences.append(f"{name}: only in {a}")
        elif name not in left:
            differences.append(f"{name}: only in {b}")
        else:
            one, two = left[name].read_bytes(), right[name].read_bytes()
            if one != two:
                differences.append(_describe(name, one, two))
    return differences


def _describe(name: str, one: bytes, two: bytes) -> str:
    """Where two files differ: the lines that changed, or, if the text reads the same, the bytes (line endings)."""
    lines = list(difflib.unified_diff(one.decode("utf-8", "replace").splitlines(),
                                      two.decode("utf-8", "replace").splitlines(), "before", "after", lineterm="", n=0))
    changed = [line for line in lines if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    if not changed:
        return f"{name}: the text reads the same but the bytes differ ({len(one)} against {len(two)} bytes)"
    shown = "\n    ".join(changed[:SHOWN_LINES])
    more = f"\n    ... and {len(changed) - SHOWN_LINES} more changed lines" if len(changed) > SHOWN_LINES else ""
    return f"{name}: differs\n    {shown}{more}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capture", help="capture the corpus's generated files (in the sandbox toolbox)").add_argument(
        "out", type=pathlib.Path)
    both = commands.add_parser("compare", help="compare two captures byte for byte")
    both.add_argument("before", type=pathlib.Path)
    both.add_argument("after", type=pathlib.Path)
    args = parser.parse_args(argv)

    if args.command == "capture":
        print(capture(args.out))
        return 0
    for directory in (args.before, args.after):
        if not directory.is_dir():
            parser.error(f"{directory} is not a directory")
    differences = compare(args.before, args.after)
    if differences:
        print(f"{len(differences)} difference(s) between {args.before} and {args.after}:")
        print("\n".join(differences))
        return 1
    print(f"identical: {len(_files(args.before))} files, byte for byte")
    return 0


if __name__ == "__main__":
    sys.exit(main())
