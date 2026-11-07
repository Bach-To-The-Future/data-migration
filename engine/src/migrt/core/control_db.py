"""The control database: one Postgres database per engagement (spec section 14, ADR 0007).

Deleting a client's data is dropping their database. Snapshots are immutable: triggers
refuse every UPDATE, DELETE and TRUNCATE on them, and reading one back recomputes every
content hash and the snapshot's identifier, so rows added to a stored snapshot are caught
too. This is a first cut of the tables; the run catalog is widened in later work.
"""
from __future__ import annotations

import contextlib
import json
import re

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from .catalog import CatalogObject, Fact, Provenance, Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    id            text PRIMARY KEY,
    source        text NOT NULL,
    object_count  integer NOT NULL,
    stored_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS catalog_object (
    snapshot_id   text NOT NULL REFERENCES snapshot (id),
    identity      text NOT NULL,
    kind          text NOT NULL,
    content_hash  text NOT NULL,
    PRIMARY KEY (snapshot_id, identity)
);
CREATE TABLE IF NOT EXISTS catalog_fact (
    snapshot_id        text NOT NULL,
    identity           text NOT NULL,
    area               text NOT NULL,
    name               text NOT NULL,
    value              jsonb NOT NULL,
    provenance_kind    text NOT NULL CHECK (provenance_kind IN ('server', 'file', 'assumed')),
    provenance_detail  text NOT NULL CHECK (provenance_detail <> ''),
    PRIMARY KEY (snapshot_id, identity, area, name),
    FOREIGN KEY (snapshot_id, identity) REFERENCES catalog_object (snapshot_id, identity)
);
-- Every extraction is recorded, including one that produced an existing snapshot again.
CREATE TABLE IF NOT EXISTS extraction (
    id            bigserial PRIMARY KEY,
    snapshot_id   text NOT NULL REFERENCES snapshot (id),
    source_description  text NOT NULL,
    extracted_at  timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION refuse_snapshot_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'snapshots are immutable: % on % refused', TG_OP, TG_TABLE_NAME;
END $$;
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['snapshot', 'catalog_object', 'catalog_fact', 'extraction'] LOOP
        EXECUTE format('CREATE OR REPLACE TRIGGER %I BEFORE UPDATE OR DELETE ON %I
                        FOR EACH ROW EXECUTE FUNCTION refuse_snapshot_change()', t || '_immutable', t);
        EXECUTE format('CREATE OR REPLACE TRIGGER %I BEFORE TRUNCATE ON %I
                        FOR EACH STATEMENT EXECUTE FUNCTION refuse_snapshot_change()', t || '_no_truncate', t);
    END LOOP;
END $$;

-- The run catalog, first cut: one row per run and per artefact in it, append-only.
CREATE TABLE IF NOT EXISTS run (
    id            text PRIMARY KEY,
    snapshot_id   text NOT NULL REFERENCES snapshot (id),
    preview       boolean NOT NULL,
    started_at    timestamptz NOT NULL,
    finished_at   timestamptz NOT NULL,
    report_data_sha256  text NOT NULL
);
CREATE TABLE IF NOT EXISTS run_artefact (
    run_id             text NOT NULL REFERENCES run (id),
    identity           text NOT NULL,
    ir_format_version  text NOT NULL,
    ir_document        jsonb NOT NULL,
    catalog_reads      jsonb NOT NULL,
    catalog_reads_sha256  text NOT NULL,
    generated_path     text NOT NULL,
    generated_sha256   text NOT NULL,
    links              jsonb NOT NULL,
    layer1             jsonb NOT NULL,
    status             text NOT NULL,
    PRIMARY KEY (run_id, identity)
);
"""


class SnapshotCorrupted(Exception):
    """A stored snapshot no longer hashes to what it was stored as."""


def database_name(engagement: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]{1,50}", engagement):
        raise ValueError("an engagement name is 1 to 50 letters, digits or underscores")
    return "migrt_" + engagement.lower()


def _dsn_for(server_dsn: str, dbname: str) -> str:
    params = conninfo_to_dict(server_dsn)
    params["dbname"] = dbname
    return make_conninfo(**params)


def ensure_database(server_dsn: str, engagement: str) -> str:
    """Create the engagement's database if missing and bring its tables up to date.
    Returns the connection string for it."""
    name = database_name(engagement)
    with psycopg.connect(server_dsn, autocommit=True) as admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if not exists:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = _dsn_for(server_dsn, name)
    with psycopg.connect(dsn) as conn:
        conn.execute(SCHEMA)
    return dsn


def drop_database(server_dsn: str, engagement: str) -> None:
    """Delete everything the tool holds for one engagement."""
    name = database_name(engagement)
    with psycopg.connect(server_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@contextlib.contextmanager
def connect(dsn: str):
    with psycopg.connect(dsn) as conn:
        yield conn


def store_snapshot(dsn: str, snapshot: Snapshot, source_description: str) -> bool:
    """Store a snapshot unless it is already stored, and record the extraction either way.
    Returns True when the snapshot is new."""
    with connect(dsn) as conn, conn.transaction():
        new = conn.execute("SELECT 1 FROM snapshot WHERE id = %s", (snapshot.id,)).fetchone() is None
        if new:
            conn.execute("INSERT INTO snapshot (id, source, object_count) VALUES (%s, %s, %s)",
                         (snapshot.id, snapshot.source, len(snapshot.objects)))
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO catalog_object (snapshot_id, identity, kind, content_hash) VALUES (%s, %s, %s, %s)",
                    [(snapshot.id, o.identity, o.kind, o.content_hash) for o in snapshot.objects])
                cur.executemany(
                    "INSERT INTO catalog_fact (snapshot_id, identity, area, name, value, provenance_kind,"
                    " provenance_detail) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    [(snapshot.id, o.identity, f.area, f.name, json.dumps(f.value, default=str),
                      f.provenance.kind, f.provenance.detail) for o in snapshot.objects for f in o.facts])
        conn.execute("INSERT INTO extraction (snapshot_id, source_description) VALUES (%s, %s)",
                     (snapshot.id, source_description))
    return new


def load_snapshot(dsn: str, snapshot_id: str) -> Snapshot:
    """Read a snapshot back, checking every object's content hash and the identifier."""
    with connect(dsn) as conn:
        head = conn.execute("SELECT source FROM snapshot WHERE id = %s", (snapshot_id,)).fetchone()
        if head is None:
            raise KeyError(f"no snapshot {snapshot_id}")
        objects = conn.execute(
            "SELECT identity, kind, content_hash FROM catalog_object WHERE snapshot_id = %s ORDER BY identity",
            (snapshot_id,)).fetchall()
        facts: dict[str, list[Fact]] = {}
        for identity, area, name, value, pkind, pdetail in conn.execute(
                "SELECT identity, area, name, value, provenance_kind, provenance_detail FROM catalog_fact"
                " WHERE snapshot_id = %s ORDER BY identity, area, name", (snapshot_id,)):
            facts.setdefault(identity, []).append(Fact(area, name, value, Provenance(pkind, pdetail)))
    loaded = []
    for identity, kind, stored_hash in objects:
        obj = CatalogObject(identity, kind, tuple(facts.get(identity, [])))
        if obj.content_hash != stored_hash:
            raise SnapshotCorrupted(f"{identity} in {snapshot_id} no longer matches its content hash")
        loaded.append(obj)
    snapshot = Snapshot.of(head[0], loaded)
    if snapshot.id != snapshot_id:
        raise SnapshotCorrupted(f"snapshot {snapshot_id} holds objects that were not in it when stored")
    return snapshot


def record_run(dsn: str, run: dict, artefacts: list[dict]) -> None:
    with connect(dsn) as conn, conn.transaction():
        conn.execute(
            "INSERT INTO run (id, snapshot_id, preview, started_at, finished_at, report_data_sha256)"
            " VALUES (%(id)s, %(snapshot_id)s, %(preview)s, %(started_at)s, %(finished_at)s, %(report_data_sha256)s)",
            run)
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO run_artefact (run_id, identity, ir_format_version, ir_document, catalog_reads,"
                " catalog_reads_sha256, generated_path, generated_sha256, links, layer1, status) VALUES"
                " (%(run_id)s, %(identity)s, %(ir_format_version)s, %(ir_document)s, %(catalog_reads)s,"
                " %(catalog_reads_sha256)s, %(generated_path)s, %(generated_sha256)s, %(links)s, %(layer1)s,"
                " %(status)s)",
                artefacts)
