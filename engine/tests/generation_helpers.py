"""Generation of SQL text against the corpus snapshot, as the engine does it: lower, check, generate, then
layer 1. Names are adjusted by the rules the engine records, built here for the identities given."""
from __future__ import annotations

import dataclasses

from lowering_helpers import lower_text

from migrt.core import checker, name_adjustments
from migrt.core.differences import Catalogue
from migrt.core.ir import Artefact
from migrt.sources.sqlserver import differences as source_differences, lowering
from migrt.targets.pyspark import differences as target_differences, generator
from migrt.targets.pyspark.layer1 import Layer1Result
from migrt.targets.pyspark import names as target_names

def names_for(snapshot, objects: dict[str, str]) -> target_names.Names:
    """The name rules for the snapshot's objects and the ones given as {identity: catalog kind}, as extraction would
    record them: a schema object's parts are its dotted identity, a query batch's its database, no schema and its
    file's name."""
    parts: dict[str, tuple[str, str, str]] = {}
    kinds: dict[str, str] = {}
    for o in snapshot.objects:
        if o.has_fact("name_parts"):
            parts[o.identity] = tuple(o.fact("name_parts").value)       # type: ignore[assignment]
            kinds[o.identity] = o.kind
    for identity, kind in objects.items():
        kinds[identity] = kind
        if identity.startswith("batch:"):
            parts[identity] = ("RetailSample", "", identity.removeprefix("batch:").removesuffix(".sql"))
        else:
            database, schema, name = identity.split(".")
            parts[identity] = (database, schema, name)
    scopes = target_names.scopes([(*parts[i], kind) for i, kind in kinds.items()])
    recorded = {key: name_adjustments.extend(target_names.RULES[key[0]], names, {}) for key, names in scopes.items()}
    return target_names.Names(recorded, parts)


@dataclasses.dataclass
class Generated:
    artefact: Artefact
    file: generator.GeneratedFile
    layer1: Layer1Result

    @property
    def text(self) -> str:
        return self.file.text


def generate_text(snapshot, sql: str, kind: str = "view", name: str = "vTest", schema: str = "sales",
                  generated_in_project: dict[str, str] | None = None, dependency_kinds: dict[str, str] | None = None,
                  **lowering_arguments) -> Generated:
    """`generated_in_project` is the other artefacts of the project, {identity: catalog kind}; `dependency_kinds`
    the catalog kind of everything else the artefact reads."""
    artefact, reader = lower_text(snapshot, sql, kind=kind, name=name, schema=schema, **lowering_arguments)
    assert checker.check(artefact, lowering.columns_of(reader)) == []
    names = names_for(snapshot, {artefact.identity: kind, **(generated_in_project or {})})
    dependencies = generator.Dependencies(kinds={**(dependency_kinds or {}), **(generated_in_project or {})},
                                          generated=set(generated_in_project or {}))
    differences = Catalogue(source_differences.half(), target_differences.half())
    file = generator.generate(artefact, "snapshot-test", "0.0.0", differences, names, dependencies)
    return Generated(artefact, file, generator.check(artefact, file))
