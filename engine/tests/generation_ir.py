"""Small hand-built representations for tests of the generator's own behaviour, with no database and no snapshot.

Lowering is not what these tests are about, so the nodes are written out as lowering conventions (issue #42) give
them: a view's body holds a `query`, a plan step holds its `input`, its `condition` or its `output`s, an expression
holds its `argument`s. The helpers number the nodes and give each a source position, as lowering does.
"""
from __future__ import annotations

import dataclasses
import types

from generation_helpers import names_for

from migrt.core.differences import Catalogue
from migrt.core.ir import Artefact, Node, SourceSpan, VType
from migrt.sources.sqlserver import differences as source_differences
from migrt.targets.pyspark import differences as target_differences, generator

TABLE = "RetailSample.sales.Order"
VIEW = "RetailSample.sales.vTest"
FUNCTION = "RetailSample.ref.fnDouble"
INT = VType.of("integer", bits=32, signed=True)
STRING = VType.of("string", length=20, unit="bytes", fixed=False, collation="any", origin="column")
MONEY = VType.of("money", precision=19)
BOOLEAN = VType.of("boolean")
# A type with no neutral meaning: the target has no Spark type for it, and detection marks every node typed by it.
NO_TYPE = VType.of("unsupported_type", source_name="geography")


def node(kind: str, role: str = "", *children: Node, type_: VType | None = None, **attrs) -> Node:
    return Node(kind, None, type_, dict(attrs), list(children), role)


def column(role: str, name: str, type_: VType = INT, alias: str | None = None, relation: str = "o") -> Node:
    return node("column", role, type_=type_, relation=relation, column=name, output_name=alias or name)


def money_product(role: str = "argument") -> Node:
    """Always manual work: the target has no money type yet."""
    return node("money_multiply", role, column("argument", "Price", MONEY), column("argument", "Rate", MONEY),
                type_=MONEY)


def count_rows(role: str = "argument") -> Node:
    return node("count_rows", role, type_=INT)


def read_orders() -> Node:
    return node("read_table", "input", table=TABLE, alias="o")


def view(*steps: Node) -> Artefact:
    """A view whose query is the plan step given."""
    [query] = steps
    query.role = "query"
    body = node("view_body", "", query)
    artefact = Artefact(VIEW, "view", "", body, dependencies=[TABLE, FUNCTION])
    artefact.secret_settings = [f"{VIEW}.secret_1"]
    _position(artefact)
    return artefact


def _position(artefact: Artefact) -> None:
    artefact.number_nodes()
    for n in artefact.nodes():
        offset = int(n.id[1:]) * 10
        n.span = SourceSpan(1, offset, 1, offset + 5, offset, 5)


@dataclasses.dataclass
class Result:
    artefact: Artefact
    file: generator.GeneratedFile

    @property
    def text(self) -> str:
        return self.file.text

    @property
    def layer1(self):
        return generator.check(self.artefact, self.file)

    @property
    def records(self) -> list[str]:
        """Everything the report can say about what generation did not do exactly."""
        return [*self.file.assumptions, *(m.reason for m in self.file.manual_items)]


def generate(artefact: Artefact) -> Result:
    objects = {artefact.identity: "view", TABLE: "table", FUNCTION: "scalar_function"}
    names = names_for(types.SimpleNamespace(objects=[]), objects)
    dependencies = generator.Dependencies(kinds={TABLE: "table", FUNCTION: "scalar_function"}, generated={FUNCTION})
    differences = Catalogue(source_differences.half(), target_differences.half())
    return Result(artefact, generator.generate(artefact, "snapshot-test", "0.0.0", differences, names, dependencies))
