"""SSIS packages, a recognised artefact type (spec sections 1 and 3): read as XML with the
standard library and inventoried, never converted.

The inventory: the package's connection managers with their connection strings, its
variables and parameters, its tasks (containers and event handlers flattened, each with its
path), the data-flow components of each pipeline, the precedence constraints, and the SQL
the package sends with the connection it sends it on, and the tables its data flows read
or write. References from that SQL to catalog objects are worked out by the extractor with
the parser. Both package formats are read: attributes (format 8, SQL Server 2012 and later)
and property elements (format 3, 2008).

A package saved with "encrypt all" (protection levels 3 and 4) cannot be inventoried without
its key; it is recorded as present, with the reason, and is manual work like any other.
Likely secrets are marked: every connection string whole, every value the package marks
sensitive, and the T-SQL patterns in the SQL and the variables' values.
"""
from __future__ import annotations

import dataclasses
import re
import xml.etree.ElementTree as ET

from . import secrets

DTS = "www.microsoft.com/SqlServer/Dts"
SQLTASK = "www.microsoft.com/sqlserver/dts/tasks/sqltask"
PROTECTION_LEVELS = {0: "DontSaveSensitive", 1: "EncryptSensitiveWithUserKey", 2: "EncryptSensitiveWithPassword",
                     3: "EncryptAllWithPassword", 4: "EncryptAllWithUserKey", 5: "ServerStorage"}
ENCRYPT_ALL = {3, 4}
# Data-flow component properties that name a table or hold SQL.
TABLE_PROPERTIES = ("OpenRowset", "TableOrViewName")
SQL_PROPERTIES = ("SqlCommand",)
_CATALOG = re.compile(r"(?:^|;)\s*(?:Initial Catalog|Database)\s*=\s*([^;]+)", re.I)


def _a(name: str) -> str:
    return f"{{{DTS}}}{name}"


def _prop(element: ET.Element, name: str) -> str | None:
    """A DTS property, as an attribute (format 8) or a DTS:Property child (format 3)."""
    value = element.get(_a(name))
    if value is not None:
        return value
    for child in element.findall(f"{{{DTS}}}Property"):
        if child.get(_a("Name")) == name:
            return child.text or ""
    return None


@dataclasses.dataclass
class Inventory:
    """What one package holds. `facts` are the catalog facts' values by name; `sql` and
    `tables` are where the package names catalog objects, for the extractor to resolve."""
    facts: dict
    sql: list[dict]          # {"task", "component", "connection", "database", "text", "fact", "row"}
    tables: list[dict]       # {"task", "component", "connection", "database", "name"}
    marked: list[dict]       # the `secrets` fact


class _Reader:
    def __init__(self, root: ET.Element):
        self.root = root
        self.connections: list[dict] = []
        self.sensitive: list[dict] = []
        self.tasks: list[dict] = []
        self.components: list[dict] = []
        self.precedence: list[dict] = []
        self.by_id: dict[str, str] = {}      # DTSID or refId -> connection manager name
        self.catalog: dict[str, str | None] = {}   # connection name -> its Initial Catalog

    def read_connections(self) -> None:
        for cm in self.root.iter(_a("ConnectionManager")):
            name = _prop(cm, "ObjectName")
            if name is None:
                continue   # the inner element that holds a manager's own properties
            inner = cm.find(f"{{{DTS}}}ObjectData/{{{DTS}}}ConnectionManager")
            string = _prop(inner, "ConnectionString") if inner is not None else None
            if string is None:
                string = _prop(cm, "ConnectionString")
            expression = next((e.text for e in cm.findall(f"{{{DTS}}}PropertyExpression")
                               if e.get(_a("Name")) == "ConnectionString"), None)
            entry = {"name": name, "type": _prop(cm, "CreationName"), "id": _prop(cm, "DTSID"),
                     "connection_string": string, "expression": expression}
            self.connections.append(entry)
            for key in (_prop(cm, "DTSID"), cm.get(_a("refId"))):
                if key:
                    self.by_id[key] = name
            found = _CATALOG.search(string or "")
            self.catalog[name] = found.group(1).strip() if found else None
            for element in (inner if inner is not None else cm).iter():
                if element.get(_a("Sensitive")) == "1" or element.get("Sensitive") == "1":
                    self.sensitive.append({"connection": name, "property": element.get(_a("Name")) or
                                           element.tag.split("}")[-1], "value": element.text or ""})

    def connection(self, key: str | None) -> str | None:
        return None if key is None else self.by_id.get(key, key)

    def read_executables(self, parent: ET.Element, path: str) -> None:
        for holder in (parent.find(f"{{{DTS}}}Executables"),):
            for task in (holder if holder is not None else []):
                if task.tag != _a("Executable"):
                    continue
                name = _prop(task, "ObjectName") or ""
                task_path = task.get(_a("refId")) or f"{path}\\{name}"
                entry = {"path": task_path, "name": name,
                         "type": _prop(task, "ExecutableType") or _prop(task, "CreationName"),
                         "disabled": (_prop(task, "Disabled") or "").casefold() == "true",
                         "connection": None, "sql": None, "sql_source_type": None}
                data = task.find(f"{{{DTS}}}ObjectData")
                sql_task = data.find(f"{{{SQLTASK}}}SqlTaskData") if data is not None else None
                if sql_task is not None:
                    entry["connection"] = self.connection(sql_task.get(f"{{{SQLTASK}}}Connection"))
                    entry["sql"] = sql_task.get(f"{{{SQLTASK}}}SqlStatementSource")
                    entry["sql_source_type"] = sql_task.get(f"{{{SQLTASK}}}SqlStatementSourceType") or "DirectInput"
                self.tasks.append(entry)
                pipeline = data.find("pipeline") if data is not None else None
                if pipeline is not None:
                    self.read_pipeline(pipeline, task_path)
                self.read_executables(task, task_path)
                self.read_precedence(task)
                self.read_event_handlers(task, task_path)

    def read_event_handlers(self, element: ET.Element, path: str) -> None:
        handlers = element.find(f"{{{DTS}}}EventHandlers")
        for handler in (handlers if handlers is not None else []):
            event = _prop(handler, "EventName") or _prop(handler, "ObjectName") or ""
            handler_path = handler.get(_a("refId")) or f"{path}.EventHandlers[{event}]"
            self.read_executables(handler, handler_path)
            self.read_precedence(handler)

    def read_pipeline(self, pipeline: ET.Element, task_path: str) -> None:
        for component in pipeline.iter("component"):
            props = {p.get("name"): p.text for p in component.iter("property") if p.get("name")}
            connections = [self.connection(c.get("connectionManagerRefId") or c.get("connectionManagerID"))
                           for c in component.iter("connection")]
            self.components.append({
                "task": task_path, "name": component.get("name"), "class": component.get("componentClassID"),
                "connection": connections[0] if connections else None,
                "table": next((props[k] for k in TABLE_PROPERTIES if props.get(k)), None),
                "sql": next((props[k] for k in SQL_PROPERTIES if props.get(k)), None),
                "variable": props.get("VariableName") or None,
                "sql_variable": props.get("SqlCommandVariable") or props.get("OpenRowsetVariable") or None})

    def read_precedence(self, element: ET.Element) -> None:
        holder = element.find(f"{{{DTS}}}PrecedenceConstraints")
        for pc in (holder if holder is not None else []):
            self.precedence.append({"from": _prop(pc, "From"), "to": _prop(pc, "To"),
                                    "value": _prop(pc, "Value") or "0", "eval_op": _prop(pc, "EvalOp"),
                                    "expression": _prop(pc, "Expression"),
                                    "logical_and": (_prop(pc, "LogicalAnd") or "True").casefold() == "true"})


def inventory(text: str) -> Inventory:
    """The package's inventory. Raises ET.ParseError for a file that is not well-formed XML."""
    root = ET.fromstring(text)
    level_text = _prop(root, "ProtectionLevel")
    level = int(level_text) if level_text and level_text.isdigit() else 1   # SSIS's default: EncryptSensitiveWithUserKey
    formats = [p.text for p in root.findall(f"{{{DTS}}}Property") if p.get(_a("Name")) == "PackageFormatVersion"]
    facts: dict = {"package_name": _prop(root, "ObjectName"),
                   "protection_level": {"value": level, "name": PROTECTION_LEVELS.get(level, "unknown"),
                                        "stated": level_text is not None},
                   "format_version": formats[0] if formats else None}
    encrypted = level in ENCRYPT_ALL or root.find(f"{{{DTS}}}EncryptedData") is not None or root.find(
        "EncryptedData") is not None
    if encrypted:
        facts["inventoried"] = False
        facts["not_inventoried_reason"] = (f"saved with \"encrypt all\" ({PROTECTION_LEVELS.get(level, level)}): its "
                                           "contents cannot be read without the key")
        return Inventory(facts, [], [], [])
    reader = _Reader(root)
    reader.read_connections()
    variables = []
    for v in root.iter(_a("Variable")):
        held = v.find(f"{{{DTS}}}VariableValue")
        variables.append({"namespace": _prop(v, "Namespace"), "name": _prop(v, "ObjectName"),
                          "expression": _prop(v, "Expression"),
                          "data_type": held.get(_a("DataType")) if held is not None else None,
                          "value": held.text if held is not None else None})
    parameters = []
    for p in root.iter(_a("PackageParameter")):
        value = next((e.text for e in p.iter(f"{{{DTS}}}Property") if e.get(_a("Name")) == "ParameterValue"), None)
        parameters.append({"name": _prop(p, "ObjectName"), "data_type": _prop(p, "DataType"),
                           "sensitive": _prop(p, "Sensitive") in ("True", "1"), "value": value})
    reader.read_executables(root, "Package")
    reader.read_precedence(root)
    reader.read_event_handlers(root, "Package")
    facts.update({"inventoried": True, "connections": reader.connections, "variables": variables,
                  "parameters": parameters, "tasks": reader.tasks, "components": reader.components,
                  "precedence_constraints": reader.precedence, "sensitive_properties": reader.sensitive})

    marked: list[dict] = []

    def mark(fact: str, spans, row: int, column: str) -> None:
        marked.extend({"fact": fact, **dataclasses.replace(s, row=row, column=column).to_json()} for s in spans)

    for row, c in enumerate(reader.connections):
        mark("connections", secrets.whole(c["connection_string"], "connection string"), row, "connection_string")
        mark("connections", secrets.whole(c["expression"], "connection string"), row, "expression")
    for row, s in enumerate(reader.sensitive):
        mark("sensitive_properties", secrets.whole(s["value"], "sensitive property"), row, "value")
    for row, v in enumerate(variables):
        mark("variables", secrets.in_text(v["value"] or ""), row, "value")
    for row, p in enumerate(parameters):
        spans = secrets.whole(p["value"], "sensitive property") if p["sensitive"] else secrets.in_text(p["value"] or "")
        mark("parameters", spans, row, "value")
    for row, t in enumerate(reader.tasks):
        mark("tasks", secrets.in_text(t["sql"] or ""), row, "sql")
    for row, c in enumerate(reader.components):
        mark("components", secrets.in_text(c["sql"] or ""), row, "sql")

    sql, tables = [], []
    for row, t in enumerate(reader.tasks):
        if t["sql"] and t["sql_source_type"] == "DirectInput":
            sql.append({"task": t["path"], "component": None, "connection": t["connection"],
                        "database": reader.catalog.get(t["connection"] or ""), "text": t["sql"], "fact": "tasks",
                        "row": row})
    for row, c in enumerate(reader.components):
        where = {"task": c["task"], "component": c["name"], "connection": c["connection"],
                 "database": reader.catalog.get(c["connection"] or "")}
        if c["sql"]:
            sql.append({**where, "text": c["sql"], "fact": "components", "row": row})
        if c["table"]:
            tables.append({**where, "name": c["table"]})
    return Inventory(facts, sql, tables, marked)


def unread_sql(facts: dict) -> list[dict]:
    """Tasks and components whose SQL or table is not in the package (read from a file
    connection or a variable), so their references cannot be worked out."""
    return ([{"task": t["path"], "reason": f"its SQL comes from {t['sql_source_type']}: not in the package"}
             for t in facts.get("tasks", []) if t["sql"] and t["sql_source_type"] not in (None, "DirectInput")] +
            [{"task": c["task"], "component": c["name"],
              "reason": f"its SQL or table comes from the variable {c['sql_variable']}: not in the package"}
             for c in facts.get("components", []) if c["sql_variable"]])
