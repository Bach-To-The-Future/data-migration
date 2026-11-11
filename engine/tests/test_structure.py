"""The rules that keep the engine's shape (spec sections 2 and 17): the command line holds
no conversion logic and reaches the engine only through its library interface, and the
core imports nothing from a source or a target."""
import ast
import json
import os
import pathlib

from migrt import cli
from migrt.core import vocabulary

PACKAGE = pathlib.Path(cli.__file__).parent
REPO = PACKAGE.parents[2]


def imports_of(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            package = ".".join(path.relative_to(PACKAGE.parent).with_suffix("").parts[:-node.level or None])
            base = (package + "." if node.level else "") + (node.module or "")
            found |= {base} if node.module else {f"{base}{a.name}" for a in node.names}
    return found


def test_the_command_line_reaches_the_engine_only_through_its_library_interface():
    internal = {i for i in imports_of(PACKAGE / "cli.py") if i.startswith("migrt")}
    assert internal == {"migrt.engine"}


def test_the_core_imports_nothing_from_a_source_or_a_target():
    for path in (PACKAGE / "core").glob("*.py"):
        bad = {i for i in imports_of(path) if i.startswith(("migrt.sources", "migrt.targets"))}
        assert not bad, f"{path.name} imports {bad}"


def test_the_support_library_imports_nothing_from_the_engine():
    """It ships on its own in every delivered project (ADR 0018), and shares no code with the testing module or the
    final validator (CODING_STANDARDS.md, Independence)."""
    library = PACKAGE.parent / "migrt_spark"
    assert library.is_dir()
    for path in library.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and not n.level}
        assert not {i for i in imported if i == "migrt" or i.startswith("migrt.")}, path.name


def test_the_cores_vocabulary_is_the_specs():
    spec = json.loads((REPO / "docs" / "spec-drafts" / "operation-vocabulary.json").read_text(encoding="utf-8"))
    assert json.loads(vocabulary.PATH.read_text(encoding="utf-8")) == spec


def test_one_command_generates_one_view_as_a_preview(tmp_path, capsys, engagement):
    code = cli.main(["preview", "--engagement", engagement.name, "--control-dsn", os.environ["MIGRT_CONTROL_DSN"],
                     "--database", "RetailSample", "--artefact", "RetailSample.sales.vOrderTotals",
                     "--out", str(tmp_path)])

    summary = json.loads(capsys.readouterr().out)
    assert code == 0
    assert summary["preview"] is True
    assert [a["status"] for a in summary["artefacts"]] == ["Not reconciled"]
    assert (tmp_path / "converted/RetailSample/sales/views/vOrderTotals.py").is_file()
