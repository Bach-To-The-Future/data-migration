"""How far the corpus procedures and triggers are from generating whole: the numbers every ticket report quotes before and after.

For each procedure and trigger of the three corpus databases it generates the artefact and lists what still stops it, and
prints:

- the root blockers remaining: one for each (procedure, kind of construct) pair, with the cascades collapsed (a statement the
  generator handles that is refused only because a scope barrier made its scope uncertain is not a blocker of its own: the
  barrier is);
- the procedures with at most 1 and at most 3 blockers left, and how many generate whole, against the ceiling (a
  procedure with dynamic SQL or a schema change is manual work for good, so it is never whole);
- the table by waiting ticket: the procedures each ticket stops, and the ones it is the only remaining ticket for;
- with `--full`, the table by kind of construct and the table by procedure.

A statement reports only its first failure, so what is counted is the construct that failed plus every kind with no
generation beneath it. It is a lower bound: a gap hidden by a decline (a type, a collation) shows up when the shallower ones
are fixed, and the total can rise.

    run in the sandbox toolbox (it extracts the corpus from the sandbox SQL Server):
        docker compose -f sandbox/docker-compose.yml --profile tools run --no-deps --rm -T tools \\
            python tools/corpus_blockers.py [--full] [--json before.json] [--compare before.json]

`--json PATH` writes the measurement; `--compare PATH` prints each headline number against an earlier one. A ticket report
quotes the output before and after, and `engine/tests/test_corpus_blockers.py` pins the numbers.
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATABASES = ["RetailSample", "AdventureWorks2022", "WideWorldImporters"]
FOR_GOOD = frozenset({"dynamic_sql_execution", "schema_change"})      # manual work for good: such a procedure is never whole
NO_TICKET = "none"
CAVEAT = ("caveat: lower bound. A statement reports only its first failure, so a gap hidden under it (a type, a collation, a "
          "deeper construct) shows up when the shallower ones are fixed, and the total can rise.")


@dataclasses.dataclass(frozen=True)
class Measurement:
    blockers: dict[str, frozenset[str]]      # procedure or trigger -> the kinds of construct that still stop it
    tickets: dict[str, tuple[str, ...]]      # kind -> the tickets it waits for (`none` where no ticket has taken it)


@dataclasses.dataclass(frozen=True)
class TicketRow:
    ticket: str
    procedures: int       # procedures it stops
    only: int             # procedures it is the only remaining ticket for


@dataclasses.dataclass(frozen=True)
class Summary:
    procedures: int
    root_blockers: int
    at_most_1: int
    at_most_3: int
    whole: int
    ceiling: int
    by_ticket: list[TicketRow]
    by_kind: list[tuple[str, tuple[str, ...], int, int]]       # kind, tickets, procedures it stops, procedures it alone stops


def tickets_of(kind: str, tickets: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    return tickets.get(kind) or (NO_TICKET,)


def summarise(m: Measurement) -> Summary:
    blockers = m.blockers
    per_kind: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for kinds in blockers.values():
        for kind in kinds:
            per_kind[kind][0] += 1
            per_kind[kind][1] += len(kinds) == 1
    stopped: dict[str, int] = collections.Counter()
    only: dict[str, int] = collections.Counter()
    for kinds in blockers.values():
        waiting = {t for kind in kinds for t in tickets_of(kind, m.tickets)}
        for t in waiting:
            stopped[t] += 1
        if len(waiting) == 1:
            only[next(iter(waiting))] += 1
    rows = sorted((TicketRow(t, n, only[t]) for t, n in stopped.items()), key=lambda r: (-r.procedures, r.ticket))
    kinds = sorted(((k, tickets_of(k, m.tickets), n, s) for k, (n, s) in per_kind.items()), key=lambda r: (-r[2], r[0]))
    return Summary(
        procedures=len(blockers), root_blockers=sum(len(k) for k in blockers.values()),
        at_most_1=sum(len(k) <= 1 for k in blockers.values()), at_most_3=sum(len(k) <= 3 for k in blockers.values()),
        whole=sum(not k for k in blockers.values()), ceiling=sum(not (k & FOR_GOOD) for k in blockers.values()),
        by_ticket=rows, by_kind=kinds)


def headline(s: Summary) -> list[str]:
    return [f"procedures and triggers measured: {s.procedures}",
            f"root blockers remaining: {s.root_blockers}",
            f"procedures with at most 1 left: {s.at_most_1}; at most 3 left: {s.at_most_3}",
            f"whole: {s.whole} of {s.procedures} (ceiling {s.ceiling}: the rest hold dynamic SQL or a schema change)"]


def report(m: Measurement, *, full: bool = False) -> str:
    s = summarise(m)
    lines = [*headline(s), "", CAVEAT, "", "By waiting ticket", "",
             "| ticket | procedures it stops | only remaining ticket for |", "|---|---|---|"]
    lines += [f"| {r.ticket} | {r.procedures} | {r.only} |" for r in s.by_ticket]
    if full:
        lines += ["", "By kind of construct", "",
                  "| blocker kind | waiting ticket | procedures it stops | procedures it alone stops |", "|---|---|---|---|"]
        lines += [f"| {k} | {', '.join(t)} | {n} | {only} |" for k, t, n, only in s.by_kind]
        lines += ["", "By procedure", "", "| procedure | distinct blockers | blockers |", "|---|---|---|"]
        for name, kinds in sorted(m.blockers.items(), key=lambda kv: (len(kv[1]), kv[0])):
            lines.append(f"| {name} | {len(kinds)} | {', '.join(sorted(kinds))} |")
    return "\n".join(lines) + "\n"


def compare(before: Measurement, after: Measurement) -> str:
    a, b = summarise(before), summarise(after)
    pairs = [("root blockers remaining", a.root_blockers, b.root_blockers),
             ("procedures with at most 1 left", a.at_most_1, b.at_most_1),
             ("procedures with at most 3 left", a.at_most_3, b.at_most_3), ("whole", a.whole, b.whole)]
    return "\n".join(f"{name}: {x} -> {y}" for name, x, y in pairs) + "\n"


# ---------------------------------------------------------------- measuring the corpus

def blockers_of(artefact, generated) -> set[str]:
    """The kinds of construct that stop one generated artefact: each manual item's own node, and every kind with no generation
    beneath the statement it replaced. A node of a kind the generator handles that is refused only because a scope barrier made
    its scope uncertain is the barrier's cascade, not a blocker of its own."""
    sys.path[:0] = [str(ROOT / "engine" / "src")]
    from migrt.targets.pyspark import generator

    handled = generator.handled_kinds()
    nodes = {n.id: n for n in artefact.nodes()}
    kinds: set[str] = set()
    for item in generated.manual_items:
        node, replaced = nodes[item.node], nodes[item.replaces]
        if not ("scope_may_be_stale" in node.attrs and node.kind in handled):
            kinds.add(node.kind)
        kinds |= {n.kind for n in replaced.walk() if n.kind not in handled and n.kind != node.kind}
    return kinds


def measure(snapshot) -> Measurement:
    """Generate every procedure and trigger of a corpus snapshot, as the engine does, and list what stops each."""
    sys.path[:0] = [str(ROOT / "engine" / "src"), str(ROOT / "engine" / "tests")]
    from generation_helpers import names_for         # the recorded name rules for a snapshot, with no control database

    from migrt.core.catalog import CatalogReader
    from migrt.core.differences import Catalogue
    from migrt.sources.sqlserver import differences as source_differences, extractor, lowering, names, statement_effects
    from migrt.targets.pyspark import differences as target_differences, generator, untranslatable

    reader = CatalogReader(snapshot, names.resolve)
    catalogue = Catalogue(source_differences.half(), target_differences.half())
    from migrt.sources.sqlserver import parser

    blockers: dict[str, frozenset[str]] = {}
    waits: dict[str, set[str]] = collections.defaultdict(set)
    for o in sorted((o for o in snapshot.objects if o.kind in ("procedure", "trigger")), key=lambda o: o.identity):
        if not reader.value(o.identity, "extractable"):
            continue
        database = o.identity.split(".", 1)[0]
        parsed = parser.parse(o.identity, reader.value(o.identity, "source_text"),
                              reader.value(database, "compatibility_level", extractor.SOURCE),
                              reader.value(o.identity, "quoted_identifier", extractor.SOURCE))
        artefact = lowering.lower_artefact(reader, o.identity, parsed)
        generated = generator.generate(artefact, snapshot.id, "0.0.0", catalogue, names_for(snapshot, {o.identity: o.kind}),
                                       generator.Dependencies(), statement_effects.effect)
        found = blockers_of(artefact, generated)
        blockers[o.identity] = frozenset(found)
        nodes = {n.id: n for n in artefact.nodes()}
        for item in generated.manual_items:
            waits[nodes[item.node].kind] |= set(item.waits_for)
    handled = generator.handled_kinds()
    tickets: dict[str, tuple[str, ...]] = {}
    for kind in {k for kinds in blockers.values() for k in kinds}:
        known = set(waits.get(kind, ())) or set(untranslatable.waits_for_kind(kind, handled))
        tickets[kind] = tuple(sorted(known)) or (NO_TICKET,)
    return Measurement(blockers, tickets)


def to_json(m: Measurement) -> str:
    return json.dumps({"blockers": {p: sorted(k) for p, k in sorted(m.blockers.items())},
                       "tickets": {k: list(t) for k, t in sorted(m.tickets.items())}}, indent=1, sort_keys=True)


def from_json(text: str) -> Measurement:
    data = json.loads(text)
    return Measurement({p: frozenset(k) for p, k in data["blockers"].items()}, {k: tuple(t) for k, t in data["tickets"].items()})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--full", action="store_true", help="also print the tables by kind and by procedure")
    parser.add_argument("--json", type=pathlib.Path, help="write the measurement here")
    parser.add_argument("--compare", type=pathlib.Path, help="print the headline numbers against an earlier --json file")
    args = parser.parse_args(argv)

    sys.path[:0] = [str(ROOT / "engine" / "src")]
    from migrt.sources.sqlserver import extractor

    address = extractor.ServerAddress(os.environ.get("MSSQL_HOST", "sqlserver"), 1433, "migration_extractor",
                                      os.environ["MIGRT_EXTRACTOR_PASSWORD"])
    measured = measure(extractor.extract(address, DATABASES))
    if args.json:
        args.json.write_text(to_json(measured) + "\n", encoding="utf-8")
    print(report(measured, full=args.full), end="")
    if args.compare:
        print("\nAgainst " + str(args.compare) + "\n\n" + compare(from_json(args.compare.read_text(encoding="utf-8")), measured), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
