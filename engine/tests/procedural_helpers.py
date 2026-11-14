"""Shared by the tests of procedural lowering: a procedure body lowered against the corpus snapshot and checked."""
from __future__ import annotations

from migrt.core import checker
from migrt.sources.sqlserver import lowering

from lowering_helpers import lower_text

PROC = "CREATE PROCEDURE sales.uspTest {params} AS BEGIN\n{body}\nEND"


def proc(snapshot, body: str, params: str = "", parameters=()):
    return lower_text(snapshot, PROC.format(params=params, body=body), kind="procedure", name="uspTest",
                      parameters=list(parameters))


def script(snapshot, text: str):
    """A script of batches (a query batch file)."""
    return lower_text(snapshot, text, kind="query_batch", name="script")


def trigger(snapshot, body: str):
    return lower_text(snapshot, f"CREATE TRIGGER sales.trTest ON sales.[Order] AFTER UPDATE AS BEGIN\n{body}\nEND",
                      kind="trigger", name="trTest")


def checked(artefact, reader) -> list:
    return checker.check(artefact, lowering.columns_of(reader))


def lowered(snapshot, body: str, params: str = "", parameters=()):
    artefact, reader = proc(snapshot, body, params, parameters)
    assert checked(artefact, reader) == [], checked(artefact, reader)
    return artefact


def lowered_script(snapshot, text: str):
    artefact, reader = script(snapshot, text)
    assert checked(artefact, reader) == [], checked(artefact, reader)
    return artefact


def statements(node, role: str = "statement"):
    return node.children_with(role)


def kinds(nodes) -> list[str]:
    return [n.kind for n in nodes]


def scope_of(node) -> dict:
    return node.attrs["scope"]


def opaque_statements(artefact):
    return [n for n in artefact.nodes() if n.kind == "opaque_fragment" and n.role in
            ("statement", "then", "else", "body", "handler")]
