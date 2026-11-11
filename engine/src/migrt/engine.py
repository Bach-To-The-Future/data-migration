"""The engine's library interface (spec section 2): everything the tool does is reachable
from here, with structured results. Front ends, such as the command line, hold no
conversion logic and call only this module.

A preview run (reconciliation off) of views, functions, procedures, triggers and query batches, through every layer: extraction
from one SQL Server (its self-check first), folders of files, or both; an immutable
snapshot in the engagement's control database with the name adjustments it extends;
lowering, the checker, generation, layer-1 checks, the delivered project (layout, manifest, settings,
representation rules) and the report data, which lists every SSIS package and SQL Agent job as manual work.
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
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
from .targets.pyspark import differences as target_differences, generator, project
from .targets.pyspark.layer1 import Layer1Result
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
    # Names this run's scratch databases, so sessions sharing one sandbox can tell theirs apart.
    label: str = ""

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
    layer1: Layer1Result
    manual_items: list[generator.ManualItem]


@dataclasses.dataclass(frozen=True)
class _Converted:
    ir: Artefact
    reads: CatalogReader
    file: generator.GeneratedFile
    layer1: Layer1Result
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
        sandbox.host, sandbox.port, sandbox.login, sandbox.password, sandbox.label))
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
    """identity -> (database, schema, name, kind), for every object named in a schema, and for every query batch
    read from a folder: it has no schema, and is named by its file's path inside the database's folder, without
    the extension (the target's name rules say how that becomes a module name)."""
    found = {o.identity: (*o.fact("name_parts").value, o.kind) for o in snapshot.objects if o.has_fact("name_parts")}
    for o in snapshot.objects:
        if o.kind == "query_batch":
            database, label = str(o.fact("database").value), str(o.fact("file").value)
            found[o.identity] = (database, "", os.path.splitext(label.removeprefix(f"{database}/"))[0], o.kind)
    return found


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

    results, report_items, run_rows, units = [], [], [], []
    project_kinds = _project_kinds(snapshot, artefacts)
    for identity in artefacts:
        converted = _convert(snapshot, identity, differences, target_names.Names(adjusted, parts), project_kinds)
        units.append(project.Unit(converted.ir, converted.file))
        results.append(ArtefactResult(identity, report.NOT_RECONCILED, out_dir / converted.file.relative_path,
                                      converted.layer1, converted.file.manual_items))
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
    written = project.write(out_dir, engagement=engagement.name, snapshot_id=snapshot.id, tool_version=TOOL_VERSION,
                            semantic=_semantic_settings(snapshot), units=units)

    server = next(o for o in snapshot.objects if o.kind == "server")
    findings = extraction_findings(snapshot)
    degradations = [{"kind": "skipped check", "where": f"database {name}",
                     "text": f"Dependency cross-check skipped: {check['reason']}."}
                    for name, check in sorted(findings["dependency_cross_check"]["databases"].items())
                    if check["status"] == "skipped"]
    degradations += _extraction_degradations(findings)
    degradations.append(SETTINGS_ASSUMPTION)
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
        project={"manifest": "manifest.json", "settings": "settings.json",
                 "representation_rules": "representation-rules.md", "entry_points": len(units),
                 "files": written},
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


CONVERTED_KINDS = {"view", "scalar_function", "inline_function", "table_function", "procedure", "trigger",
                   "query_batch"}
# Kinds another generated artefact can import: a function of session state returning a DataFrame or a Column.
CALLABLE_KINDS = {"view", "scalar_function", "inline_function", "table_function"}
SETTINGS_ASSUMPTION = {
    "kind": "assumption", "where": "settings.json",
    "text": "DATEFIRST, DATEFORMAT and the language were not read from the server (the extractor does not read "
            "them): the settings file holds SQL Server's defaults for us_english, marked as assumed. Set them to the "
            "client's values before verifying; changing a semantic setting means generating and verifying again."}


def _resolve(reader: CatalogReader, snapshot: Snapshot, identity: str) -> str:
    """The catalog identity of an artefact named as database.schema.name, or batch:<database>/<file>."""
    if identity.startswith("batch:"):
        if not snapshot.has(identity):
            raise KeyError(f"{identity} is not in snapshot {snapshot.id}")
        return identity
    database = identity.split(".", 1)[0]
    case_sensitive = names.identifiers_case_sensitive(reader.value(database, "collation"))
    found = reader.resolve(tuple(identity.split(".")), {"database": database, "case_sensitive": case_sensitive})
    if found is None:
        raise KeyError(f"{identity} is not in snapshot {snapshot.id}")
    return found


def _project_kinds(snapshot: Snapshot, artefacts: list[str]) -> dict[str, str]:
    """The catalog kind of each artefact the run generates, which is what a dependency between them needs."""
    reader = CatalogReader(snapshot, names.resolve)
    found = {}
    for identity in artefacts:
        resolved = _resolve(reader, snapshot, identity)
        found[resolved] = reader.kind_of(resolved)
    return found


def _origin(fact) -> str:
    kind = fact.provenance.kind
    if kind == "server":
        return "read from the server"
    return f"{'read from' if kind == 'file' else kind}: {fact.provenance.detail}"


def _semantic_settings(snapshot: Snapshot) -> dict:
    """The semantic settings the catalog snapshot and the engagement give (spec section 7), each with where it
    came from: read from the server, from a file, or assumed."""
    server = next(o for o in snapshot.objects if o.kind == "server")
    zone = server.fact("time_zone")
    databases = {}
    for o in sorted((o for o in snapshot.objects if o.kind == "database"), key=lambda o: o.identity):
        collation, level = o.fact("collation"), o.fact("compatibility_level", extractor.SOURCE)
        databases[o.identity] = {"default_collation": {"value": collation.value, "origin": _origin(collation)},
                                 "compatibility_level": {"value": level.value, "origin": _origin(level)}}
    not_read = "assumed: not read from the server; SQL Server's default for the us_english language"
    return {
        "databases": databases,
        "time_zone": {"value": zone.value, "origin": _origin(zone)},
        "datefirst": {"value": 7, "origin": not_read},
        "dateformat": {"value": "mdy", "origin": not_read},
        "language": {"value": "us_english", "origin": not_read},
        "transaction_mechanism": {"value": "compensating restore", "origin": "default (ADR 0002)"},
        "identity_mechanism": {"value": "write wrapper counters", "origin": "default (spec section 6)"},
    }


def _convert(snapshot: Snapshot, identity: str, differences: Catalogue, adjusted_names: target_names.Names,
             project_kinds: dict[str, str]) -> _Converted:
    """Lower, check, generate and run layer 1 for one artefact."""
    reader = CatalogReader(snapshot, names.resolve)
    found = _resolve(reader, snapshot, identity)
    kind = reader.kind_of(found)
    if kind not in CONVERTED_KINDS:
        raise ValueError(f"{found} is a {kind}; only {', '.join(sorted(CONVERTED_KINDS))} are converted so far")
    if not reader.value(found, "extractable"):
        raise ValueError(f"{found} is encrypted or unreadable: manual work, not handled yet")
    database = str(reader.value(found, "database")) if kind == "query_batch" else found.split(".", 1)[0]
    parsed = parser.parse(found, reader.value(found, "source_text"),
                          reader.value(database, "compatibility_level", extractor.SOURCE),
                          reader.value(found, "quoted_identifier", extractor.SOURCE))
    artefact = lowering.lower_artefact(reader, found, parsed)
    checker.require(artefact, lowering.columns_of(reader))
    dependencies = generator.Dependencies(
        kinds={dep: reader.kind_of(dep) for dep in artefact.dependencies if snapshot.has(dep)},
        generated={i for i, k in project_kinds.items() if k in CALLABLE_KINDS and i != found})
    generated = generator.generate(artefact, snapshot.id, TOOL_VERSION, differences, adjusted_names, dependencies)
    checks = generator.check(artefact, generated)

    manual_nodes = _manual_by_node(artefact, generated.manual_items)
    constructs = []
    for node in artefact.nodes():
        if vocabulary.part_of(node.kind) is None and node.kind not in REPORTED_KINDS:
            continue  # node kinds such as the view body are not constructs
        manual = manual_nodes.get(node.id)
        entry = {"node": node.id, "operation": node.kind, "source_position": str(node.span),
                 "status": report.CONSTRUCT_MANUAL if manual else report.CONSTRUCT_NOT_RECONCILED}
        if manual:
            entry["reason"], entry["manual_item"] = manual
        if "scope_may_be_stale" in node.attrs:      # shown at the construct (spec section 4, item 5)
            entry["scope_may_be_stale"] = f"a scope barrier at {node.attrs['scope_may_be_stale']} comes before it"
        if node.id in generated.differences:
            entry["difference"] = generated.differences[node.id]
            entry["caveat"] = differences.difference(entry["difference"])
        constructs.append(entry)
    area = found.split(".")[1] if kind != "query_batch" else "batches"
    reads_setting = ("A likely secret in the source. Generated code shows this named setting in its place and "
                     "reads its value from the project's settings file, to be filled in at deployment.")
    unread_setting = ("A likely secret in the source. Generated code shows this named setting in its place; no "
                      "generated code needs its value, but the settings file lists it.")
    item = report.preview_artefact(
        name=found, artefact_type=kind, area=area, output=generated.relative_path,
        layer1=checks.to_json(), constructs=constructs,
        degradations=[{"kind": "assumption", "where": found, "text": text}
                      for text in [*artefact.assumptions, *generated.assumptions]],
        hints=[{**h, "status": report.HINT_NOT_APPLICABLE} for h in artefact.hints],
        secrets=[{"setting": name, "text": reads_setting if name in generated.settings_used else unread_setting}
                 for name in artefact.secret_settings],
        name_adjustments=generated.name_adjustments)
    return _Converted(artefact, reader, generated, checks, item)


def _manual_by_node(artefact: Artefact, items: list[generator.ManualItem]) -> dict[str, tuple[str, str]]:
    """node id -> (reason, manual item id), for every node whose code the file does not hold. A node under the one
    that failed gets that item's reason; a node the failure replaced elsewhere in the query is reported as not
    generated because of it. The most specific item wins."""
    nodes = {n.id: n for n in artefact.nodes()}
    found: dict[str, tuple[str, str]] = {}
    for item in sorted(items, key=lambda m: -sum(1 for _ in nodes[m.replaces].walk())):
        for n in nodes[item.replaces].walk():
            found[n.id] = (f"its code is not generated, because of manual work {item.id[1:]} ({item.source_position})",
                           item.id)
        for n in nodes[item.node].walk():
            found[n.id] = (item.reason, item.id)
    return found
