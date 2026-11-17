"""`tools/tickets.py` lists the build tickets: the sub-issues of #35, and the sub-issues of those (GitHub holds at most 100 under
a parent, so later tickets sit under an earlier one). It needs no network here: `gh` is replaced."""
import importlib.util
import pathlib
import sys

TOOL = pathlib.Path(__file__).resolve().parents[2] / "tools" / "tickets.py"
spec = importlib.util.spec_from_file_location("tickets", TOOL)
assert spec is not None and spec.loader is not None
tickets = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tickets
spec.loader.exec_module(tickets)


def issue(number: int, children: int = 0, state: str = "open"):
    return {"number": number, "title": f"t{number}", "state": state, "labels": [], "sub_issues_summary": {"total": children}}


def test_a_ticket_under_a_ticket_is_listed_and_its_blockers_are_read(monkeypatch):
    tree = {"issues/35/sub_issues": [issue(1, children=2), issue(2)], "issues/1/sub_issues": [issue(3), issue(4)]}
    blockers = {"issues/3/dependencies/blocked_by": [issue(2)]}

    def fake(path: str):
        return tree.get(path) or blockers.get(path) or []

    monkeypatch.setattr(tickets, "gh", fake)

    found = tickets.load(35)

    assert [t["number"] for t in found] == [1, 2, 3, 4]
    assert [t for t in found if t["number"] == 3][0]["blocked_by"] == [2]
    assert [t for t in found if t["number"] == 2][0]["blocks"] == [3]


def test_a_ticket_that_is_its_own_ancestor_is_listed_once(monkeypatch):
    tree = {"issues/35/sub_issues": [issue(1, children=1)], "issues/1/sub_issues": [issue(1, children=1)]}
    monkeypatch.setattr(tickets, "gh", lambda path: tree.get(path, []))

    assert [t["number"] for t in tickets.load(35)] == [1]
