"""SQL Agent jobs, a recognised artefact type (spec sections 1 and 3): inventoried, never
converted. A live server's jobs are read from msdb (extractor._agent_jobs); a folder's are
read from job scripts, the `sp_add_job` calls SQL Server Management Studio scripts a job as.

From either, each job's references to catalog objects are worked out from its steps: a
T-SQL step's command is parsed and its names resolved in the step's database, and an SSIS
step names the package it runs, matched to a package in the extraction's folders. These
show which procedures and packages are entry points. Steps of other subsystems (CmdExec,
PowerShell and the rest) are not read for references, and each is listed with the reason.
"""
from __future__ import annotations

import dataclasses
import json
import re

from ...core.catalog import Snapshot
from . import parser, resolution, secrets

COMMAND_SUBSYSTEMS = {"CmdExec", "PowerShell", "SSIS"}
TIMING = ("enabled", "freq_type", "freq_interval", "freq_subday_type", "freq_subday_interval",
          "freq_relative_interval", "freq_recurrence_factor", "active_start_date", "active_end_date",
          "active_start_time", "active_end_time")
# msdb's own parameter defaults (read from the procedures' definitions on SQL Server 2022 CU27),
# which a script that leaves an argument out gets. Each one used is recorded as assumed.
JOB_DEFAULTS = {"enabled": 1, "description": None, "start_step_id": 1, "category_name": "[Uncategorized (Local)]"}
STEP_DEFAULTS = {"subsystem": "TSQL", "command": None, "database_name": None, "on_success_action": 1,
                 "on_fail_action": 2, "retry_attempts": 0}
JOBSCHEDULE_DEFAULTS = {"enabled": 1, "freq_type": 1, "freq_interval": 0, "freq_subday_type": 0,
                        "freq_subday_interval": 0, "freq_relative_interval": 0, "freq_recurrence_factor": 0,
                        "active_start_date": None, "active_end_date": 99991231, "active_start_time": 0,
                        "active_end_time": 235959}
SCHEDULE_DEFAULTS = {**JOBSCHEDULE_DEFAULTS, "freq_type": 0}
DEFAULT_REASON = "not in the script: the default of msdb's {procedure}"
START_DATE_REASON = "not in the script: SQL Server sets it to the day the schedule is created"
# SQL Agent runs T-SQL steps with QUOTED_IDENTIFIER OFF, so a step is parsed that way.
STEP_QUOTED_IDENTIFIER = False
STEP_DEFAULT_DATABASE = "master"   # a T-SQL step with no database runs in master
# Procedures a job script calls that the inventory does not need, and that change no job.
SCAFFOLDING = {"sp_add_jobserver", "sp_delete_job", "sp_add_category", "sp_help_job", "sp_verify_job_identifiers"}
JOB_PROCEDURES = {"sp_add_job", "sp_add_jobstep", "sp_add_jobschedule", "sp_add_schedule", "sp_attach_schedule"}
# The arguments a script passes as variables to tie its calls together; any other variable
# is a value the inventory cannot read.
VARIABLE_ARGUMENTS = {"job_id", "schedule_id"}
_PACKAGE = re.compile(r"/(?:FILE|F|SQL|ISSERVER|DTS)\s+(\"(?:[^\"\\]|\\.)*\"|\S+)", re.I)


def _name(fragment) -> str:
    return str(fragment.GetType().Name)


class _Unreadable(Exception):
    """An argument whose value the script computes, so the inventory cannot read it."""


def _value(fragment, text: str):
    kind = _name(fragment)
    if kind == "StringLiteral":
        return str(fragment.Value)
    if kind == "IntegerLiteral":
        return int(str(fragment.Value))
    if kind == "NullLiteral":
        return None
    if kind == "UnaryExpression" and _name(fragment.Expression) == "IntegerLiteral":
        return int(re.sub(r"[\s()]", "", text[int(fragment.StartOffset):int(fragment.StartOffset) +
                                              int(fragment.FragmentLength)]))
    if kind == "VariableReference":
        return ("variable", str(fragment.Name))
    raise _Unreadable(f"an expression ({kind})")


@dataclasses.dataclass
class ScriptedJob:
    name: str
    line: int
    values: dict
    steps: list[dict] = dataclasses.field(default_factory=list)
    schedules: list[dict] = dataclasses.field(default_factory=list)
    assumed: list[dict] = dataclasses.field(default_factory=list)
    not_interpreted: list[dict] = dataclasses.field(default_factory=list)


def from_script(parsed: parser.Parsed) -> tuple[list[ScriptedJob], list[dict]]:
    """The jobs a script creates, from its sp_add_job, sp_add_jobstep, sp_add_jobschedule,
    sp_add_schedule and sp_attach_schedule calls with named arguments. Returns the jobs and
    every call the inventory could not read, with the reason (which is never dropped)."""
    calls, seen = [], set()

    def visit(f) -> None:
        # The visitor is called once per type in a node's hierarchy: keep each call once.
        if _name(f) == "ExecuteSpecification" and (int(f.StartOffset), int(f.FragmentLength)) not in seen:
            seen.add((int(f.StartOffset), int(f.FragmentLength)))
            calls.append(f)
    parser.walk(parsed.tree, visit)
    jobs: dict[str, ScriptedJob] = {}
    by_variable: dict[str, str] = {}             # @jobId -> job name
    schedules: dict[str, dict] = {}              # sp_add_schedule's schedules, by name and by @variable
    unread: list[dict] = []
    text = parsed.text

    def note(line: int, procedure: str, reason: str) -> None:
        unread.append({"line": line, "procedure": procedure, "reason": reason})

    for call in calls:
        entity = call.ExecutableEntity
        if _name(entity) != "ExecutableProcedureReference" or entity.ProcedureReference is None:
            continue
        name = entity.ProcedureReference.ProcedureReference.Name
        procedure = str(name.BaseIdentifier.Value).casefold()
        line = int(call.StartLine)
        if procedure in SCAFFOLDING or not procedure.startswith("sp_"):
            continue
        if procedure not in JOB_PROCEDURES:
            if procedure.startswith(("sp_update_job", "sp_update_schedule", "sp_delete_jobstep", "sp_add_alert",
                                     "sp_add_notification", "sp_detach_schedule")):
                note(line, procedure, "a change to a job the inventory does not replay")
            continue
        args: dict = {}
        try:
            for p in entity.Parameters:
                if p.Variable is None:
                    raise _Unreadable("a positional argument: the inventory reads named arguments only")
                key = str(p.Variable.Name).casefold().lstrip("@")
                value = _value(p.ParameterValue, text)
                if isinstance(value, tuple) and key not in VARIABLE_ARGUMENTS:
                    raise _Unreadable(f"@{key} is a variable")
                args[key] = value
        except _Unreadable as unreadable:
            # Named, never quoted: an argument's text may hold a secret.
            note(line, procedure, f"an argument the inventory cannot read: {unreadable}")
            continue

        def job_of() -> ScriptedJob | None:
            named = args.get("job_name")
            if isinstance(named, str) and named in jobs:
                return jobs[named]
            ident = args.get("job_id")
            if isinstance(ident, tuple) and ident[1] in by_variable:
                return jobs[by_variable[ident[1]]]
            note(line, procedure, "names no job this script creates")
            return None

        def defaults(table: dict, where: dict) -> tuple[dict, list[dict]]:
            """The arguments with msdb's defaults filled in, and an assumed value for each
            default used; `where` says which step or schedule it belongs to."""
            values, assumed = {}, []
            for key, default in table.items():
                values[key] = args.get(key, default)
                if key not in args:
                    assumed.append({**where, "property": key, "value": default,
                                    "reason": START_DATE_REASON if key == "active_start_date" else
                                    DEFAULT_REASON.format(procedure=procedure)})
            return values, assumed

        if procedure == "sp_add_job":
            job_name = args.get("job_name")
            if not isinstance(job_name, str):
                note(line, procedure, "the job's name is not a literal")
                continue
            if job_name in jobs:
                raise DuplicateJob(job_name)
            values, assumed = defaults(JOB_DEFAULTS, {"fact": "job"})
            jobs[job_name] = ScriptedJob(job_name, line, values, assumed=assumed)
            if isinstance(args.get("job_id"), tuple):
                by_variable[args["job_id"][1]] = job_name
        elif procedure == "sp_add_jobstep":
            job = job_of()
            if job is None:
                continue
            step_id = args.get("step_id", len(job.steps) + 1)
            values, assumed = defaults(STEP_DEFAULTS, {"fact": "steps", "step_id": step_id})
            if "step_id" not in args:
                assumed.append({"fact": "steps", "step_id": step_id, "property": "step_id", "value": step_id,
                                "reason": "not in the script: msdb numbers steps in the order they are added"})
            job.assumed += assumed
            job.steps.append({"step_id": step_id, "name": args.get("step_name"), "subsystem": values["subsystem"],
                              "command": values["command"], "database": values["database_name"],
                              "on_success_action": values["on_success_action"],
                              "on_fail_action": values["on_fail_action"], "retry_attempts": values["retry_attempts"]})
        elif procedure == "sp_add_jobschedule":
            job = job_of()
            if job is None:
                continue
            values, assumed = defaults(JOBSCHEDULE_DEFAULTS, {"fact": "schedules", "schedule": args.get("name")})
            schedule = {"name": args.get("name"), **{k: values[k] for k in TIMING}}
            job.assumed += assumed + _verified(schedule)
            job.schedules.append(schedule)
        elif procedure == "sp_add_schedule":
            name = args.get("schedule_name")
            values, assumed = defaults(SCHEDULE_DEFAULTS, {"fact": "schedules", "schedule": name})
            schedule = {"name": name, **{k: values[k] for k in TIMING}}
            made = {"entry": schedule, "assumed": assumed + _verified(schedule)}
            schedules[str(name)] = made
            if isinstance(args.get("schedule_id"), tuple):
                schedules[args["schedule_id"][1]] = made
        elif procedure == "sp_attach_schedule":
            job = job_of()
            if job is None:
                continue
            ident = args.get("schedule_id")
            made = schedules.get(ident[1] if isinstance(ident, tuple) else str(args.get("schedule_name")))
            if made is None:
                note(line, procedure, "attaches a schedule this script does not create")
                continue
            job.schedules.append(dict(made["entry"]))
            job.assumed += made["assumed"]
    for job in jobs.values():
        job.steps.sort(key=lambda s: s["step_id"])
        job.schedules.sort(key=_canonical)
    return sorted(jobs.values(), key=lambda j: j.name), unread


VERIFY_REASON = "msdb's sp_verify_schedule sets it before storing the schedule"


def _verified(schedule: dict) -> list[dict]:
    """What msdb's sp_verify_schedule does to a schedule before storing it (read from its
    definition on SQL Server 2022 CU27), applied in place, with an assumed value for each
    change: one-time, start-up and idle schedules keep no recurrence; any other schedule with
    no sub-day type runs once a day; a daily schedule has no recurrence factor."""
    before = dict(schedule)
    if schedule["freq_type"] in (0x1, 0x40, 0x80):
        schedule.update(freq_interval=0, freq_subday_type=0, freq_subday_interval=0, freq_relative_interval=0,
                        freq_recurrence_factor=0)
    else:
        if schedule["freq_subday_type"] == 0:
            schedule["freq_subday_type"] = 0x1
        if schedule["freq_type"] == 0x4:
            schedule["freq_recurrence_factor"] = 0
    return [{"fact": "schedules", "schedule": schedule["name"], "property": key, "value": schedule[key],
             "reason": VERIFY_REASON} for key in schedule if schedule[key] != before[key]]


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class DuplicateJob(Exception):
    def __init__(self, name: str):
        self.name = name
        super().__init__(f"the SQL Agent job {name!r} is created twice")


def marked_steps(steps: list[dict]) -> list[dict]:
    """Likely secrets in the steps' commands, as a job's `secrets` fact holds them."""
    marked = []
    for index, s in enumerate(steps):
        spans = secrets.in_text(s["command"] or "", command_line=s["subsystem"] in COMMAND_SUBSYSTEMS)
        marked += [{"fact": "steps", **dataclasses.replace(span, row=index, column="command").to_json()}
                   for span in spans]
    return marked


@dataclasses.dataclass(frozen=True)
class StepDatabase:
    """What a step's T-SQL is parsed and resolved with, for a database in the engagement."""
    compatibility_level: int
    case_sensitive: bool
    system_types: frozenset[str]
    schemas: frozenset[str]


def package_path(command: str) -> tuple[str, int] | None:
    """The package an SSIS step runs (/FILE, /SQL, /ISSERVER or /DTS), and where it is."""
    found = _PACKAGE.search(command or "")
    if found is None:
        return None
    raw = found.group(1).replace('\\"', '"').strip('"').strip()
    return raw, found.start(1)


def package_stem(path: str) -> str:
    stem = re.split(r"[\\/]", path.rstrip("\\/"))[-1]
    return (stem[:-5] if stem.casefold().endswith(".dtsx") else stem).casefold()


def step_references(steps: list[dict], snapshot: Snapshot, databases: dict[str, StepDatabase], *,
                    default_level: int, system_types: frozenset[str], packages: dict[str, list[str]],
                    engagement: set[str], job: str) -> tuple[list[dict], list[dict]]:
    """Every step's references, with its step_id, and every step that could not be read for
    references, with the reason. `packages` maps a package's file stem, folded, to its
    catalog identities."""
    found: list[dict] = []
    unavailable: list[dict] = []
    for step in steps:
        sid, subsystem, command = step["step_id"], step["subsystem"], step["command"] or ""
        if subsystem == "SSIS":
            located = package_path(command)
            if located is None:
                unavailable.append({"step_id": sid, "reason": "an SSIS step whose command names no package"})
                continue
            path, offset = located
            matches = packages.get(package_stem(path), [])
            line = command.count("\n", 0, offset) + 1
            column = offset - (command.rfind("\n", 0, offset) + 1) + 1
            found.append({"step_id": sid, "name": [path], "namespace": "ssis_package", "line": line,
                          "column": column, "resolved": matches[0] if len(matches) == 1 else None, "via": None,
                          "external": (None if len(matches) == 1 else
                                       f"ambiguous: {path} matches {', '.join(matches)}" if matches else
                                       "a package not in the extraction's folders")})
            continue
        if subsystem != "TSQL":
            unavailable.append({"step_id": sid, "reason": f"a {subsystem} step: its command is not T-SQL, and is "
                                                          "not read for references"})
            continue
        database = step["database"] or STEP_DEFAULT_DATABASE
        known = next((d for name, d in databases.items() if name.casefold() == database.casefold()), None)
        try:
            parsed = parser.parse(f"{job} step {sid}", command, known.compatibility_level if known else default_level,
                                  STEP_QUOTED_IDENTIFIER)
        except parser.ParseFailed as failed:
            unavailable.append({"step_id": sid, "reason": f"the command did not parse: {failed}"})
            continue
        where = resolution.context(database, "dbo", known.case_sensitive if known else False)
        for ref in resolution.work_out(parsed, snapshot, where, system_types=set(known.system_types if known
                                                                                  else system_types),
                                       schemas=set(known.schemas if known else {"dbo"}), engagement=engagement):
            if known is None and ref["resolved"] is None and ref["external"] == "not in the catalog":
                ref["external"] = f"another database: the step runs in {database}"
            found.append({"step_id": sid, **ref})
    return found, unavailable
