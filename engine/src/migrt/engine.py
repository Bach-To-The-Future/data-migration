"""The engine's library interface (spec section 2): everything the tool does is reachable
from here, with structured results. Front ends, such as the command line, hold no
conversion logic and call only this module.

First cut: a preview run (reconciliation off) of views from one SQL Server, through every
layer: the extractor's self-check, an immutable snapshot in the engagement's control
database, lowering, the checker, generation, layer-1 checks and the report data.
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import pathlib
import uuid

from .core import checker, control_db, report
from .core.differences import Catalogue
from .core.catalog import CatalogReader, Snapshot
from .core.ir import FORMAT_VERSION, Artefact
from .core import vocabulary
from .sources.sqlserver import differences as source_differences, extractor, lowering, names, parser
from .sources.sqlserver.extractor import ExtractorPermissionError
from .targets.pyspark import differences as target_differences, generator, layer1

__all__ = ["Engagement", "SqlServerSource", "ArtefactResult", "RunResult", "ExtractorPermissionError",
           "extract", "read_snapshot", "preview_run", "delete_engagement_data", "TOOL_VERSION"]

TOOL_VERSION = "0.1.0"
TARGET = {"platform": "PySpark on Databricks", "runtime": "17.3 LTS"}


def _differences() -> Catalogue:
    """The silent-differences catalogue: the source's half and the target's, read together."""
    return Catalogue(source_differences.half(), target_differences.half())


@dataclasses.dataclass(frozen=True)
class Engagement:
    """One client engagement: its name, which names its control database, and the
    Postgres server that holds that database."""
    name: str
    control_server_dsn: str


@dataclasses.dataclass(frozen=True)
class SqlServerSource:
    """A live SQL Server reached through the extractor's read-only login."""
    host: str
    port: int
    login: str
    password: str
    databases: list[str]

    def describe(self) -> str:
        return f"SQL Server {self.host},{self.port} as {self.login}: {', '.join(self.databases)}"


@dataclasses.dataclass(frozen=True)
class ArtefactResult:
    identity: str
    status: str
    file: pathlib.Path
    layer1: layer1.Layer1Result
    manual_items: list[generator.ManualItem]


@dataclasses.dataclass(frozen=True)
class _ConvertedView:
    ir: Artefact
    reads: CatalogReader
    file: generator.GeneratedFile
    layer1: layer1.Layer1Result
    report_item: dict


@dataclasses.dataclass(frozen=True)
class RunResult:
    run_id: str
    snapshot_id: str
    preview: bool
    artefacts: list[ArtefactResult]
    report_data_path: pathlib.Path


def _control(engagement: Engagement) -> str:
    return control_db.ensure_database(engagement.control_server_dsn, engagement.name)


def extract(engagement: Engagement, source: SqlServerSource) -> Snapshot:
    """Check the extractor's permissions, extract, and store the snapshot immutably.
    Raises ExtractorPermissionError, before reading anything, if a grant is missing."""
    address = extractor.ServerAddress(source.host, source.port, source.login, source.password)
    snapshot = extractor.extract(address, source.databases)
    control_db.store_snapshot(_control(engagement), snapshot, source.describe())
    return snapshot


def read_snapshot(engagement: Engagement, snapshot_id: str) -> Snapshot:
    return control_db.load_snapshot(_control(engagement), snapshot_id)


def delete_engagement_data(engagement: Engagement) -> None:
    """Delete everything the tool holds for this engagement: drop its control database."""
    control_db.drop_database(engagement.control_server_dsn, engagement.name)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def preview_run(engagement: Engagement, source: SqlServerSource, artefacts: list[str],
                out_dir: pathlib.Path) -> RunResult:
    """Convert the named artefacts (database.schema.name) with reconciliation off.
    Writes converted/<Database>/<schema>/<kind>/<Name>.py and report/report-data.json
    under out_dir. Every artefact is "Not reconciled": nothing in a preview is converted."""
    started = _now()
    run_id = f"run-{started:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    dsn = _control(engagement)
    snapshot = extract(engagement, source)
    snapshot = control_db.load_snapshot(dsn, snapshot.id)   # convert from what was stored
    out_dir = pathlib.Path(out_dir)
    differences = _differences()

    results, report_items, run_rows = [], [], []
    for identity in artefacts:
        converted = _convert_view(snapshot, identity, differences)
        path = out_dir / converted.file.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(converted.file.text, encoding="utf-8", newline="\n")
        results.append(ArtefactResult(identity, report.NOT_RECONCILED, path, converted.layer1,
                                      converted.file.manual_items))
        report_items.append(converted.report_item)
        run_rows.append({
            "run_id": run_id, "identity": identity, "ir_format_version": FORMAT_VERSION,
            "ir_document": converted.ir.document(),
            "catalog_reads": json.dumps([dataclasses.asdict(r) for r in converted.reads.reads]),
            "catalog_reads_sha256": converted.reads.reads_hash(),
            "generated_path": converted.file.relative_path,
            "generated_sha256": _sha256(converted.file.text),
            "links": json.dumps(converted.file.links), "layer1": json.dumps(converted.layer1.to_json()),
            "status": report.NOT_RECONCILED,
        })

    server = next(o for o in snapshot.objects if o.kind == "server")
    finished = _now()
    data = report.preview_report(
        engagement=engagement.name,
        snapshot=snapshot.id,
        source={"platform": "SQL Server", "version": server.fact("version").value,
                "edition": server.fact("edition").value, "collation": server.fact("collation").value},
        target=TARGET,
        versions={"tool": TOOL_VERSION, "ir_format": FORMAT_VERSION, "vocabulary": vocabulary.version(),
                  **differences.versions(), "report_data": report.SCHEMA_VERSION},
        artefacts=report_items,
        degradations=[{"kind": "skipped check", "where": "every artefact",
                       "text": "Preview run: layers 2 to 6 of the testing module and final validation did not run."},
                      {"kind": "skipped check", "where": "every artefact",
                       "text": "The preview packaging check (install the project in a fresh sandbox, import every "
                               "module, call each entry point on empty tables) is not built yet and did not run."}],
        run_specific={"run_id": run_id, "started": started.isoformat(), "finished": finished.isoformat(),
                      "duration_seconds": round((finished - started).total_seconds(), 3)},
    )
    report_path = out_dir / "report" / "report-data.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    text = report.dumps(data)
    report_path.write_text(text, encoding="utf-8", newline="\n")
    control_db.record_run(dsn, {"id": run_id, "snapshot_id": snapshot.id, "preview": True, "started_at": started,
                                "finished_at": finished, "report_data_sha256": _sha256(text)}, run_rows)
    return RunResult(run_id, snapshot.id, True, results, report_path)


def _convert_view(snapshot: Snapshot, identity: str, differences: Catalogue) -> _ConvertedView:
    """Lower, check, generate and run layer 1 for one view."""
    reader = CatalogReader(snapshot, names.resolve)
    database = identity.split(".", 1)[0]
    case_sensitive = names.identifiers_case_sensitive(reader.value(database, "collation"))
    found = reader.resolve(tuple(identity.split(".")), {"database": database, "case_sensitive": case_sensitive})
    if found is None:
        raise KeyError(f"{identity} is not in snapshot {snapshot.id}")
    if reader.kind_of(found) != "view":
        raise ValueError(f"{found} is a {reader.kind_of(found)}; only views are converted so far")
    if not reader.value(found, "extractable"):
        raise ValueError(f"{found} is encrypted or unreadable: manual work, not handled yet")
    parsed = parser.parse(found, reader.value(found, "source_text"),
                          reader.value(database, "compatibility_level", extractor.SOURCE),
                          reader.value(found, "quoted_identifier", extractor.SOURCE))
    artefact = lowering.lower_view(reader, found, parsed)
    checker.require(artefact, lambda table: {c["name"] for c in reader.value(table, "columns")})
    generated = generator.generate(artefact, snapshot.id, TOOL_VERSION, differences)
    spans = {n.id: (str(n.span) if n.span else None) for n in artefact.nodes()}
    checks = layer1.run(generated.text, links=generated.links, node_spans=spans,
                        manual_items=[m.id for m in generated.manual_items],
                        escape_hatch_uses=generated.escape_hatch_uses)

    manual_nodes = {n.id: m for m in generated.manual_items for n in _subtree(artefact, m.node)}
    constructs = []
    for node in artefact.nodes():
        if vocabulary.part_of(node.kind) is None and node.kind != "opaque_fragment":
            continue  # node kinds such as the view body are not constructs
        item = manual_nodes.get(node.id)
        entry = {"node": node.id, "operation": node.kind, "source_position": str(node.span),
                 "status": report.CONSTRUCT_MANUAL if item else report.CONSTRUCT_NOT_RECONCILED}
        if item:
            entry["reason"] = item.reason
        if node.id in generated.differences:
            entry["difference"] = generated.differences[node.id]
            entry["caveat"] = differences.difference(entry["difference"])
        constructs.append(entry)
    item = report.preview_artefact(
        name=found, artefact_type="view", area=found.split(".")[1], output=generated.relative_path,
        layer1=checks.to_json(), constructs=constructs, degradations=[])
    return _ConvertedView(artefact, reader, generated, checks, item)


def _subtree(artefact, node_id: str):
    for node in artefact.nodes():
        if node.id == node_id:
            return list(node.walk())
    return []
