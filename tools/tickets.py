"""The build tickets and their blocking graph, compactly, from GitHub.

    python tools/tickets.py                 every sub-issue of the parent (default #35): one line each
    python tools/tickets.py --ticket 87     one ticket: what blocks it and what it blocks
    python tools/tickets.py --open          only open tickets
    python tools/tickets.py --json          the same data as JSON

Always ends with a cycle check over the blocking edges, and exits 1 if there is a cycle.
Needs the gh CLI, authenticated. Reads only; changes nothing on GitHub.
"""
import argparse
import concurrent.futures
import json
import subprocess
import sys

REPO = "Bach-To-The-Future/data-migration"


def gh(path: str):
    out = subprocess.run(["gh", "api", "--paginate", f"repos/{REPO}/{path}"],
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    # --paginate concatenates JSON arrays page by page.
    return json.loads(out.replace("][", ","))


def load(parent: int) -> list[dict]:
    subs = gh(f"issues/{parent}/sub_issues")

    def edges(issue):
        blockers = gh(f"issues/{issue['number']}/dependencies/blocked_by")
        return {"number": issue["number"], "title": issue["title"], "state": issue["state"],
                "labels": sorted(label["name"] for label in issue["labels"]),
                "blocked_by": sorted(b["number"] for b in blockers),
                "open_blockers": sorted(b["number"] for b in blockers if b["state"] == "open")}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        tickets = list(pool.map(edges, subs))
    blocks: dict[int, list[int]] = {}
    for t in tickets:
        for b in t["blocked_by"]:
            blocks.setdefault(b, []).append(t["number"])
    for t in tickets:
        t["blocks"] = sorted(blocks.get(t["number"], []))
    return sorted(tickets, key=lambda t: t["number"])


def cycles(tickets: list[dict]) -> list[list[int]]:
    graph = {t["number"]: t["blocked_by"] for t in tickets}
    state: dict[int, int] = {}
    found: list[list[int]] = []

    def visit(n: int, path: list[int]) -> None:
        state[n] = 1
        path.append(n)
        for m in graph.get(n, []):
            if state.get(m) == 1:
                found.append(path[path.index(m):] + [m])
            elif m not in state:
                visit(m, path)
        path.pop()
        state[n] = 2

    sys.setrecursionlimit(10_000)
    for n in graph:
        if n not in state:
            visit(n, [])
    return found


def line(t: dict) -> str:
    gate = f" | blocked by {', '.join(f'#{b}' for b in t['open_blockers'])}" if t["open_blockers"] else ""
    return f"#{t['number']} [{t['state']}] {t['title']}{gate}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--parent", type=int, default=35)
    parser.add_argument("--ticket", type=int)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    tickets = load(args.parent)
    found = cycles(tickets)
    shown = [t for t in tickets if (not args.open or t["state"] == "open")
             and (args.ticket is None or t["number"] == args.ticket)]
    if args.json:
        print(json.dumps({"tickets": shown, "cycles": found}, indent=1))
    elif args.ticket is not None:
        if not shown:
            print(f"#{args.ticket} is not a sub-issue of #{args.parent}")
            return 1
        t = shown[0]
        print(line(t))
        print(f"  labels: {', '.join(t['labels']) or '-'}")
        print(f"  blocked by: {', '.join(f'#{b}' for b in t['blocked_by']) or '-'}")
        print(f"  blocks: {', '.join(f'#{b}' for b in t['blocks']) or '-'}")
    else:
        print("\n".join(line(t) for t in shown))
    if not args.json:
        print(f"cycles: {found or 'none'} ({sum(len(t['blocked_by']) for t in tickets)} edges)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
