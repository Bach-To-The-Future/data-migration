"""The engine's library interface (spec section 2): everything the tool does is reachable
from here, with structured results. Front ends, such as the command line, hold no
conversion logic and call only this module.

First cut: a preview run (reconciliation off) of views, through every layer: extraction
from one SQL Server (its self-check first), folders of files, or both; an immutable
snapshot in the engagement's control database with the name adjustments it extends;
lowering, the checker, generation, layer-1 checks and the report data, which lists every
SSIS package and SQL Agent job as manual work.
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import pathlib
import uuid
from typing import Sequence

from .core import checker, control_db, report, secrets
from .core.differences import Catalogue
from .core.catalog import CatalogReader, Snapshot
from .core.ir import FORMAT_VERSION, Artefact
from .core import vocabulary
from .sources.sqlserver import differences as source_differences, extractor, lowering, names, parser, shapes
from .sources.sqlserver import folders as folder_source
from .sources.sqlserver.extractor import (ExtractorPermissionError, SettingsRequired, TimeZoneRequired,
                                          UnsupportedServer)
from .sources.sqlserver.folders import DuplicateCreate, FolderExtractionError
from .sources.sqlserver.shapes import SandboxUnavailable
from .targets.pyspark import differences as target_differences, generator, layer1
from .targets.pyspark import names as target_names

__all__ = ["Engagement", "SqlServerSource", "Sandbox", "Folder", "FolderSettings", "ArtefactResult", "RunResult",
           "ExtractorPermissionError", "UnsupportedServer", "TimeZoneRequired", "SettingsRequired",
           "FolderExtractionError", "DuplicateCreate", "SandboxUnavailable", "EXTRACTION_ERRORS", "extract",
           "read_snapshot",
           "name_adjustments", "extraction_findings", "recognised_artefacts", "preview_run",
           "delete_engagement_data", "TOOL_VERSION"]

TOOL_VERSION = "0.1.0"
TARGET = {"platform": "PySpark on Databricks", "runtime": "17.3 LTS"}
# Node kinds that are not operations but are constructs the report lists (spec section 4, items 5 and 6).
REPORTED_KINDS = {"opaque_fragment", "dynamic_sql_execution", "schema_change", "permission_change",
                  "external_reference"}


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
    # The server's time zone, required only where CURRENT_TIMEZONE() fails (SQL Server 2017).
    time_zone: str | None = None

    def describe(self) -> str:
        return f"SQL Server {self.host},{self.port} as {self.login}: {', '.join(self.databases)}"


@dataclasses.dataclass(frozen=True)
class Sandbox:
    """The sandbox SQL Server, the tool's own disposable one, and a login with full rights on
    it. Procedure result shapes are described there (spec section 3); it is never the
    client's server."""
    host: str
    port: int
    login: str
    password: str

    def describe(self) -> str:
        return f"sandbox SQL Server {self.host},{self.port} as {self.login}"


@dataclasses.dataclass(frozen=True)
class Folder:
    """A folder of exported files (query batches, SSIS packages, job scripts, data
    definition), and the database its files belong to."""
    path: pathlib.Path
    database: str


@dataclasses.dataclass(frozen=True)
class FolderSettings:
    """What a server would have supplied, for databases extracted from folders alone, all
    required: each database's default collation and compatibility level, the time zone,
    and the SQL Server release."""
    collations: dict[str, str] = dataclasses.field(default_factory=dict)
    time_zone: str | None = None
    version: str | None = None
    compatibility_levels: dict[str, int] = dataclasses.field(default_factory=dict)


# Every error an extraction raises before storing anything, for front ends to report.
EXTRACTION_ERRORS = (ExtractorPermissionError, UnsupportedServer, TimeZoneRequired, SettingsRequired,
                     FolderExtractionError, SandboxUnavailable)


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


def extract(engagement: Engagement, source: SqlServerSource | None, folders: Sequence[Folder] = (),
            settings: FolderSettings | None = None, sandbox: Sandbox | None = None) -> Snapshot:
    """Extract from a live server, folders of files, or both; store the snapshot immutably,
    and extend the name adjustments for any name not recorded yet. Raises UnsupportedServer
    or ExtractorPermissionError, before reading anything, for a server v1 does not extract
    from or a grant that is missing or more than read-only; SettingsRequired without a
    server when a setting is missing; FolderExtractionError (DuplicateCreate among them)
    for folders that cannot be read as given.

    Procedure result shapes need EXECUTE, which the extractor's login never holds, so they
    are described in the `sandbox` (spec section 3), or taken from an administrator's export
    in a folder. Without either, each procedure says it was not described, with the reason.
    SandboxUnavailable is raised, before anything is stored, for a sandbox that cannot be
    reached."""
    settings = settings or FolderSettings()
    inputs = tuple(_folder_inputs(folders))
    if source is not None and settings != FolderSettings():
        raise FolderExtractionError("folder settings apply only without a server, which supplies them itself: "
                                    "remove them, or extract without the server")
    if source is not None:
        address = extractor.ServerAddress(source.host, source.port, source.login, source.password)
        snapshot = extractor.extract(address, source.databases, time_zone=source.time_zone, folder_inputs=inputs)
    else:
        options = extractor.Settings(settings.collations, settings.version, settings.compatibility_levels)
        snapshot = extractor.extract(None, [], time_zone=settings.time_zone, folder_inputs=inputs, settings=options)
    snapshot = shapes.attach(snapshot, None if sandbox is None else shapes.SandboxAddress(
        sandbox.host, sandbox.port, sandbox.login, sandbox.password))
    described = [source.describe()] if source is not None else []
    described += [f"folder {f.path} for {f.database}" for f in folders]
    described += [sandbox.describe()] if sandbox is not None else []
    dsn = _control(engagement)
    control_db.store_snapshot(dsn, snapshot, "; ".join(described))
    control_db.extend_name_adjustments(dsn, snapshot.id, target_names.scopes(list(_name_parts(snapshot).values())),
                                       target_names.RULES)
    return snapshot


def _folder_inputs(given: Sequence[Folder]) -> list[folder_source.FolderInput]:
    return [folder_source.FolderInput(pathlib.Path(f.path), f.database) for f in given]


def _name_parts(snapshot: Snapshot) -> dict[str, tuple[str, str, str, str]]:
    """identity -> (database, schema, name, kind), for every object named in a schema."""
    return {o.identity: (*o.fact("name_parts").value, o.kind) for o in snapshot.objects if o.has_fact("name_parts")}


def name_adjustments(engagement: Engagement) -> dict[tuple[str, str], dict[str, str]]:
    """The recorded name adjustments: {(rule, scope): {source name: target name}}. They
    outlive snapshots: a later extraction only adds to them."""
    return control_db.name_adjustments(_control(engagement))


def extraction_findings(snapshot: Snapshot) -> dict:
    """What an extraction found that the report must show (extractor.findings), with the
    warning that pattern matching misses some secrets."""
    found = extractor.findings(snapshot)
    found["secrets"]["note"] = secrets.MISSES_SOME
    found["result_shapes"] = shapes.findings(snapshot)
    return found


def recognised_artefacts(snapshot: Snapshot) -> list[dict]:
    """Every SSIS package and SQL Agent job, as the report lists it: manual work, with its
    inventory (marked secrets masked), references and entry points."""
    return [report.recognised_artefact(**r) for r in extractor.recognised(snapshot)]


def read_snapshot(engagement: Engagement, snapshot_id: str) -> Snapshot:
    return control_db.load_snapshot(_control(engagement), snapshot_id)


def delete_engagement_data(engagement: Engagement) -> None:
    """Delete everything the tool holds for this engagement: drop its control database."""
    control_db.drop_database(engagement.control_server_dsn, engagement.name)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def preview_run(engagement: Engagement, source: SqlServerSource | None, artefacts: list[str],
                out_dir: pathlib.Path, folders: Sequence[Folder] = (), settings: FolderSettings | None = None,
                sandbox: Sandbox | None = None) -> RunResult:
    """Convert the named artefacts (database.schema.name) with reconciliation off, extracting
    from a live server, folders of files, or both. Writes
    converted/<Database>/<schema>/<kind>/<Name>.py and report/report-data.json under out_dir.
    Every artefact is "Not reconciled": nothing in a preview is converted."""
    started = _now()
    run_id = f"run-{started:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    dsn = _control(engagement)
    snapshot = extract(engagement, source, folders, settings, sandbox)
    snapshot = control_db.load_snapshot(dsn, snapshot.id)   # convert from what was stored
    out_dir = pathlib.Path(out_dir)
    differences = _differences()
    adjusted = control_db.name_adjustments(dsn)
    parts = {identity: value[:3] for identity, value in _name_parts(snapshot).items()}

    results, report_items, run_rows = [], [], []
    for identity in artefacts:
        converted = _convert_view(snapshot, identity, differences, target_names.Names(adjusted, parts))
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
    findings = extraction_findings(snapshot)
    degradations = [{"kind": "skipped check", "where": f"database {name}",
                     "text": f"Dependency cross-check skipped: {check['reason']}."}
                    for name, check in sorted(findings["dependency_cross_check"]["databases"].items())
                    if check["status"] == "skipped"]
    degradations += _extraction_degradations(findings)
    finished = _now()
    data = report.preview_report(
        engagement=engagement.name,
        snapshot=snapshot.id,
        source={"platform": "SQL Server", "version": server.fact("version").value,
                "edition": server.fact("edition").value, "collation": server.fact("collation").value,
                "assumed": sorted(f.name for f in server.facts if f.provenance.kind == "assumed")},
        target=TARGET,
        versions={"tool": TOOL_VERSION, "ir_format": FORMAT_VERSION, "vocabulary": vocabulary.version(),
                  **differences.versions(), "report_data": report.SCHEMA_VERSION},
        artefacts=report_items,
        recognised=recognised_artefacts(snapshot),
        extraction=findings,
        degradations=degradations + [{"kind": "skipped check", "where": "every artefact",
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


def _extraction_degradations(findings: dict) -> list[dict]:
    """What the extraction could not do, or assumed, as degradations the report shows."""
    out = []
    skipped = findings["dependency_cross_check"]["skipped"]
    if skipped:
        out.append({"kind": "skipped check", "where": f"{len(skipped)} objects parsed from files",
                    "text": "Dependency cross-check skipped: SQL Server's dependency metadata covers only objects read "
                            "from a server."})
    completeness = findings["completeness"]
    if completeness["facts"]["assumed"] or completeness["assumed_values"]:
        out.append({"kind": "assumption", "where": "the catalog",
                    "text": f"{completeness['facts']['assumed']} facts, and {completeness['assumed_values']} values "
                            "inside parsed facts, were assumed from settings or SQL Server's documented defaults, not "
                            "read from a server."})
    for missing in findings["unresolved_external_references"]:
        tables = ", ".join(r["table"] for r in missing["referenced_by"])
        out.append({"kind": "reduced coverage", "where": missing["table"],
                    "text": f"Referenced by a foreign key of {tables} but not in the extraction: those tables cannot be "
                            f"recreated faithfully, and the {missing['blocked_artefacts']} artefacts that write them "
                            "are not reconcilable locally. Extend the export to include it."})
    out += _shape_degradations(findings["result_shapes"])
    folder = findings.get("folders")
    if folder:
        for role in (folder_source.NOT_READ, folder_source.DID_NOT_PARSE):
            files = [f["file"] for f in folder["files"] if f["role"] == role]
            if files:
                out.append({"kind": "reduced coverage", "where": ", ".join(files),
                            "text": f"Files {role}: nothing in them is in the catalog."})
        if folder["superseded"]:
            out.append({"kind": "reduced coverage", "where": f"{len(folder['superseded'])} statements and files",
                        "text": "Superseded by the live server, the only source of its databases' objects (ADR "
                                "0025): not replayed, each listed with its reason."})
        if folder["not_replayed"]:
            out.append({"kind": "reduced coverage", "where": f"{len(folder['not_replayed'])} statements",
                        "text": "Data-definition statements not replayed, each listed with its reason."})
        if folder["lost_without_a_server"]:
            out.append({"kind": "reduced coverage", "where": "every database read from folders alone",
                        "text": "Lost without a server: " + "; ".join(folder["lost_without_a_server"]) + "."})
    return out


def _shape_degradations(found: dict) -> list[dict]:
    """The result shapes that could not be described, and what the sandbox could not recreate."""
    out = []
    procedures = [e for e in found["could_not_be_described"] if e["procedure"]]
    others = [e for e in found["could_not_be_described"] if not e["procedure"]]
    for noun, entries in (("procedures", procedures), ("views and inline functions", others)):
        if entries:
            out.append({"kind": "reduced coverage", "where": f"{len(entries)} {noun}",
                        "text": f"The result shape of {len(entries)} {noun} could not be described, each with its "
                                "reason in the extraction section. The static check skips the shape comparison for "
                                "them, and the verdict says so (spec section 3)."})
    if found["not_recreated"]:
        out.append({"kind": "reduced coverage", "where": f"{len(found['not_recreated'])} objects",
                    "text": "The sandbox could not recreate these objects, each with SQL Server's reason in the "
                            "extraction section; shapes that depend on them could not be described."})
    if found["export_unmatched"]:
        out.append({"kind": "reduced coverage", "where": f"{len(found['export_unmatched'])} export entries",
                    "text": "Entries of an administrator's export of result shapes name procedures the catalog does "
                            "not hold; they were not used."})
    return out


def _convert_view(snapshot: Snapshot, identity: str, differences: Catalogue,
                  adjusted_names: target_names.Names) -> _ConvertedView:
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
    artefact = lowering.lower_artefact(reader, found, parsed)
    checker.require(artefact, lowering.columns_of(reader))
    generated = generator.generate(artefact, snapshot.id, TOOL_VERSION, differences, adjusted_names)
    spans = {n.id: (str(n.span) if n.span else None) for n in artefact.nodes()}
    checks = layer1.run(generated.text, links=generated.links, node_spans=spans,
                        manual_items=[m.id for m in generated.manual_items],
                        escape_hatch_uses=generated.escape_hatch_uses)

    manual_nodes = {n.id: m for m in generated.manual_items for n in _subtree(artefact, m.node)}
    constructs = []
    for node in artefact.nodes():
        if vocabulary.part_of(node.kind) is None and node.kind not in REPORTED_KINDS:
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
        layer1=checks.to_json(), constructs=constructs,
        degradations=[{"kind": "assumption", "where": found, "text": text} for text in artefact.assumptions],
        hints=[{**h, "status": report.HINT_NOT_APPLICABLE} for h in artefact.hints],
        secrets=[{"setting": name, "text": "A likely secret in the source. Generated code shows this named setting "
                                            "in its place; nothing reads settings yet (the settings file, issue #44)."}
                 for name in artefact.secret_settings],
        name_adjustments=generated.name_adjustments)
    return _ConvertedView(artefact, reader, generated, checks, item)


def _subtree(artefact, node_id: str):
    for node in artefact.nodes():
        if node.id == node_id:
            return list(node.walk())
    return []
