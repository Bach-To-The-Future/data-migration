"""Build the sandbox databases and the corpus manifest.

Runs inside the sandbox tools container; corpus/build.py is the entry point.
"""
import argparse
import hashlib
import json
import pathlib
import re
import sys

import pymssql

import constructs
import db

CORPUS = pathlib.Path(__file__).resolve().parents[1]
SAMPLE = CORPUS / "sample"
PUBLIC = CORPUS / "public"
SAMPLE_DB = "RetailSample"
SMOKE_DB = "RetailSampleSmoke"

# Folder under sample/artefacts -> artefact type. Order is creation order.
OBJECT_FOLDERS = [
    ("functions", "function"),
    ("views", "view"),
    ("procedures", "stored_procedure"),
    ("triggers", "trigger"),
]
RECOGNISED_TYPES = {"ssis_package", "agent_job"}
EXPECT_ERROR = re.compile(r"\A--\s*expect-error:\s*(.+)")

# sys.objects.type -> artefact type
OBJECT_TYPES = {
    "P": "stored_procedure",
    "V": "view",
    "FN": "function",
    "IF": "function",
    "TF": "function",
    "TR": "trigger",
}


def log(message):
    print(message, flush=True)


# ------------------------------------------------------------------ public corpus

def restore_public(force):
    spec = json.loads((PUBLIC / "backups.json").read_text(encoding="utf-8"))
    conn = db.connect()
    for backup in spec["backups"]:
        name = backup["database"]
        exists = db.query(conn, "SELECT 1 FROM sys.databases WHERE name = %s", (name,))
        if exists and not force:
            log(f"  {name}: already restored")
            continue
        if exists:
            db.execute(conn, f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{name}];")

        source = f"{db.BACKUP_DIR}/{backup['file']}"
        files = db.query(conn, f"RESTORE FILELISTONLY FROM DISK = N'{source}'")
        moves = []
        for logical, _physical, kind, *_ in files:
            # D = data, L = log, S = a container directory (filestream or memory-optimised).
            if kind == "L":
                target = f"{db.DATA_DIR}/{name}_{logical}.ldf"
            elif kind == "S":
                target = f"{db.DATA_DIR}/{name}_{logical}"
            else:
                ext = "mdf" if not moves else "ndf"
                target = f"{db.DATA_DIR}/{name}_{logical}.{ext}"
            moves.append(f"MOVE N'{logical}' TO N'{target}'")
        log(f"  {name}: restoring {len(files)} files")
        db.execute(conn, f"RESTORE DATABASE [{name}] FROM DISK = N'{source}' WITH {', '.join(moves)}, REPLACE")
        state = db.query(conn, "SELECT state_desc FROM sys.databases WHERE name = %s", (name,))
        if not state or state[0][0] != "ONLINE":
            raise RuntimeError(f"{name} did not come online after restore: {state}")
    conn.close()


def export_public():
    """Write each public artefact's source under public/export (not committed)."""
    spec = json.loads((PUBLIC / "backups.json").read_text(encoding="utf-8"))
    entries = []
    for backup in spec["backups"]:
        name = backup["database"]
        conn = db.connect(name)
        rows = db.query(conn, """
            SELECT s.name, o.name, RTRIM(o.type), m.definition
            FROM sys.sql_modules AS m
            JOIN sys.objects AS o ON o.object_id = m.object_id
            JOIN sys.schemas AS s ON s.schema_id = o.schema_id
            WHERE o.is_ms_shipped = 0
              AND RTRIM(o.type) IN ('P', 'V', 'FN', 'IF', 'TF', 'TR')
            ORDER BY s.name, o.name""")
        conn.close()
        out_dir = PUBLIC / "export" / name
        skipped = 0
        for schema, obj, obj_type, definition in rows:
            if definition is None:  # encrypted module
                skipped += 1
                continue
            kind = OBJECT_TYPES[obj_type]
            text = definition.replace("\r\n", "\n").strip() + "\n"
            path = out_dir / kind / f"{schema}.{obj}.sql"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            entries.append(entry("public", name, kind, f"{schema}.{obj}", text, path=None))
        log(f"  {name}: exported {len(rows) - skipped} artefacts" + (f", skipped {skipped} encrypted" if skipped else ""))
    return entries


# ------------------------------------------------------------------ sample corpus

def create_objects(conn):
    """Create every artefact that is a database object, retrying to settle order."""
    pending = []
    for folder, _kind in OBJECT_FOLDERS:
        pending += sorted((SAMPLE / "artefacts" / folder).glob("*.sql"))
    while pending:
        failed = []
        for path in pending:
            try:
                db.execute(conn, path.read_text(encoding="utf-8"))
            except pymssql.Error as exc:
                failed.append((path, exc))
        if len(failed) == len(pending):
            path, exc = failed[0]
            raise RuntimeError(f"{path.name} does not compile: {exc}")
        pending = [path for path, _ in failed]


def build_sample(name, with_agent_job):
    conn = db.connect()
    if db.query(conn, "SELECT 1 FROM sys.databases WHERE name = %s", (name,)):
        db.execute(conn, f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{name}];")
    db.execute(conn, f"CREATE DATABASE [{name}]")
    conn.close()

    conn = db.connect(name)
    for script in sorted((SAMPLE / "database").glob("*.sql")):
        db.run_script(conn, script.read_text(encoding="utf-8"))
    create_objects(conn)

    # Batches change data, so they are compiled here and not run.
    db.execute(conn, "SET NOEXEC ON")
    try:
        for path in sorted((SAMPLE / "artefacts" / "batches").glob("*.sql")):
            try:
                db.run_script(conn, path.read_text(encoding="utf-8"))
            except pymssql.Error as exc:
                raise RuntimeError(f"batch {path.name} does not compile: {exc}") from exc
    finally:
        db.execute(conn, "SET NOEXEC OFF")
    conn.close()

    if with_agent_job:
        conn = db.connect()
        for path in sorted((SAMPLE / "artefacts" / "recognised" / "agent_jobs").glob("*.sql")):
            db.run_script(conn, path.read_text(encoding="utf-8"))
        conn.close()


def smoke_test():
    """Run every sample artefact once on a scratch copy of the sample database."""
    build_sample(SMOKE_DB, with_agent_job=False)
    conn = db.connect(SMOKE_DB)
    try:
        for batch in db.split_batches((SAMPLE / "smoke_test.sql").read_text(encoding="utf-8")):
            expected = EXPECT_ERROR.match(batch)
            if not expected:
                db.execute(conn, batch)
                continue
            # This batch is meant to hit an error and carry on past it.
            _sets, messages = db.observe(SMOKE_DB, batch)
            errors = [text for severity, text in messages if severity > 10]
            if not any(expected.group(1) in text for text in errors):
                raise RuntimeError(f"smoke test expected an error containing {expected.group(1)!r}, got {errors}")
        for path in sorted((SAMPLE / "artefacts" / "batches").glob("*.sql")):
            db.run_script(conn, path.read_text(encoding="utf-8"))
    finally:
        conn.close()
        conn = db.connect()
        db.execute(conn, f"ALTER DATABASE [{SMOKE_DB}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{SMOKE_DB}];")
        conn.close()


def sample_entries():
    exercises = json.loads((SAMPLE / "exercises.json").read_text(encoding="utf-8"))
    entries = []
    folders = OBJECT_FOLDERS + [("batches", "batch"), ("recognised/agent_jobs", "agent_job"), ("recognised/ssis", "ssis_package")]
    for folder, kind in folders:
        for path in sorted((SAMPLE / "artefacts" / folder).glob("*.*")):
            text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
            item = entry("sample", SAMPLE_DB, kind, path.stem, text,
                         path=path.relative_to(CORPUS.parent).as_posix())
            item["exercises"] = sorted(exercises.get(path.stem, []))
            entries.append(item)
    unknown = set(exercises) - {e["name"] for e in entries} - {"_about"}
    if unknown:
        raise RuntimeError(f"exercises.json names artefacts that do not exist: {sorted(unknown)}")
    return entries


# ------------------------------------------------------------------ manifest

def entry(corpus, database, kind, name, text, path):
    is_sql = kind != "ssis_package"
    found = constructs.detect(text) if is_sql else []
    tier = "recognised" if kind in RECOGNISED_TYPES else "converted"
    item = {
        "corpus": corpus,
        "database": database,
        "type": kind,
        "tier": tier,
        "name": name,
        "code_lines": constructs.code_lines(text) if is_sql else len(text.splitlines()),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "constructs": found,
    }
    if path:
        item["path"] = path
    return item


def write_manifest(entries):
    entries.sort(key=lambda e: (e["corpus"], e["database"], e["type"], e["name"].lower()))
    summary = {}
    for e in entries:
        counts = summary.setdefault(e["corpus"], {})
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    dynamic = {}
    for e in entries:
        if "dynamic_sql" in e["constructs"]:
            dynamic[e["corpus"]] = dynamic.get(e["corpus"], 0) + 1
    spec = json.loads((PUBLIC / "backups.json").read_text(encoding="utf-8"))
    manifest = {
        "about": "Every artefact in the sample and public corpora. Generated by corpus/build.py; do not edit by hand. "
                 "Constructs are detected by pattern (corpus/tools/constructs.py) and are an index, not a parse. "
                 "Public artefacts are listed here but their source is exported to corpus/public/export at build time.",
        "public_sources": [{"database": b["database"], "file": b["file"], "sha256": b["sha256"]} for b in spec["backups"]],
        "artefact_counts": {corpus: dict(sorted(counts.items())) for corpus, counts in sorted(summary.items())},
        "artefacts_containing_dynamic_sql": dict(sorted(dynamic.items())),
        "artefacts": entries,
    }
    path = CORPUS / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-public", action="store_true")
    parser.add_argument("--force-restore", action="store_true")
    parser.add_argument("--skip-smoke", action="store_true")
    args = parser.parse_args()

    log("Waiting for SQL Server")
    db.wait_for_server()

    entries = []
    if not args.skip_public:
        log("Restoring the public corpus")
        restore_public(args.force_restore)
        entries += export_public()

    log(f"Building {SAMPLE_DB}")
    build_sample(SAMPLE_DB, with_agent_job=True)
    if not args.skip_smoke:
        log("Running every sample artefact once on a scratch copy")
        smoke_test()
    entries += sample_entries()

    if args.skip_public:
        log("Manifest not rewritten: the public corpus was skipped")
        return
    manifest = write_manifest(entries)
    log("Artefact counts:")
    for corpus, counts in manifest["artefact_counts"].items():
        log(f"  {corpus}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))


if __name__ == "__main__":
    sys.exit(main())
