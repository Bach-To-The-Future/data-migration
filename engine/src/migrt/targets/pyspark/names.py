"""The PySpark target's name rules (spec section 3, "Name adjustments outlive snapshots").

Unity Catalog refuses, even in backticks, a period, a space, a forward slash, the ASCII
control characters (00 to 1F hex) and DELETE (7F hex), and takes names of at most 255
characters; it stores every name in lowercase, so names differing only by case collide
(docs.databricks.com, sql-ref-names, checked 8 November 2026). File and module names follow
the same rule extended to Python identifiers and case-insensitive file systems: ASCII
letters, digits and `_`, not starting with a digit, never a keyword, compared ignoring case.

Scopes: catalogs (one per source database) on the server, schemas in a catalog, and tables
and views in a schema; folders for databases and schemas, and modules in a kind's folder.

A query batch read from a folder has no schema and no name parts: it is generated into `batches` under its
database's folder, and its module is named from the file's path inside the database's folder, without the
extension, each refused character (the path separator among them) becoming `_`: `batches/load.sql` is the module
`batches_load`. The name is recorded like the others, in the scope `<database>.batches`.
"""
from __future__ import annotations

import keyword
import re

from ...core.name_adjustments import Rule

UNITY_CATALOG = Rule("unity-catalog", lambda name: re.sub(r"[./ \x00-\x1f\x7f]", "_", name), str.lower,
                     max_length=255)


def _python(name: str) -> str:
    out = re.sub(r"[^A-Za-z0-9_]", "_", name) or "_"
    return "_" + out if out[0].isdigit() else out


# Names every generated module imports at module level, or binds in its functions, which a function of that name
# would shadow; and the folder a database holds its query batches in, which a schema of that name would share.
GENERATED_IMPORTS = {"f", "dataframe", "column", "ss", "spark", "batches"}
PYTHON_MODULE = Rule("python-module", _python, str.casefold,
                     lambda name: keyword.iskeyword(name) or name.casefold() in GENERATED_IMPORTS)

RULES = {rule.name: rule for rule in (UNITY_CATALOG, PYTHON_MODULE)}
# Artefact types generated into files, and their folders (spec section 6: converted/<Database>/<schema>/<kind>/<Name>.py).
KIND_FOLDERS = {"view": "views", "procedure": "procedures", "scalar_function": "functions",
                "inline_function": "functions", "table_function": "functions", "trigger": "triggers"}
BATCH_KIND, BATCH_FOLDER = "query_batch", "batches"
# What Unity Catalog holds by name: tables and views share a schema's namespace.
UC_KINDS = {"table", "view"}


def scopes(objects: list[tuple[str, str, str, str]]) -> dict[tuple[str, str], set[str]]:
    """The names to adjust, by (rule, scope), for objects given as (database, schema, name,
    kind). Scopes are written as dotted source names."""
    found: dict[tuple[str, str], set[str]] = {}

    def add(rule: Rule, scope: str, name: str) -> None:
        found.setdefault((rule.name, scope), set()).add(name)

    for database, schema, name, kind in objects:
        if kind in UC_KINDS:
            add(UNITY_CATALOG, "", database)
            add(UNITY_CATALOG, database, schema)
            add(UNITY_CATALOG, f"{database}.{schema}", name)
        if kind == BATCH_KIND:
            add(PYTHON_MODULE, "", database)
            add(PYTHON_MODULE, f"{database}.{BATCH_FOLDER}", name)
        if kind in KIND_FOLDERS:
            add(PYTHON_MODULE, "", database)
            add(PYTHON_MODULE, database, schema)
            add(PYTHON_MODULE, f"{database}.{schema}.{KIND_FOLDERS[kind]}", name)
    return found


class Names:
    """The recorded adjustments, as the generator uses them: {(rule, scope): {source: target}}."""

    def __init__(self, recorded: dict[tuple[str, str], dict[str, str]], parts: dict[str, tuple[str, str, str]]):
        self.recorded = recorded
        self.parts = parts                  # identity -> (database, schema, name), from the catalog
        # rule -> {qualified source name: target name}, for every name used that was changed
        self.used: dict[str, dict[str, str]] = {}

    def _split(self, identity: str) -> tuple[str, str, str]:
        try:
            return self.parts[identity]
        except KeyError:
            raise KeyError(f"{identity} has no recorded name parts") from None

    def _get(self, rule: Rule, scope: str, name: str) -> str:
        try:
            target = self.recorded[(rule.name, scope)][name]
        except KeyError:
            raise KeyError(f"no {rule.name} name is recorded for {name!r} in scope {scope!r}") from None
        if target != name:
            self.used.setdefault(rule.name, {})[f"{scope}.{name}" if scope else name] = target
        return target

    def table(self, identity: str) -> str:
        """The Unity Catalog name of a table or view, catalog.schema.name, in backticks."""
        database, schema, name = self._split(identity)
        parts = (self._get(UNITY_CATALOG, "", database), self._get(UNITY_CATALOG, database, schema),
                 self._get(UNITY_CATALOG, f"{database}.{schema}", name))
        return ".".join(f"`{p.replace('`', '``')}`" for p in parts)

    def module(self, identity: str, kind: str) -> tuple[str, str]:
        """The file an artefact is generated into, and its function name."""
        database, schema, name = self._split(identity)
        if kind == BATCH_KIND:
            module = self._get(PYTHON_MODULE, f"{database}.{BATCH_FOLDER}", name)
            return f"converted/{self._get(PYTHON_MODULE, '', database)}/{BATCH_FOLDER}/{module}.py", module
        folder = KIND_FOLDERS[kind]
        module = self._get(PYTHON_MODULE, f"{database}.{schema}.{folder}", name)
        path = (f"converted/{self._get(PYTHON_MODULE, '', database)}/{self._get(PYTHON_MODULE, database, schema)}"
                f"/{folder}/{module}.py")
        return path, module
