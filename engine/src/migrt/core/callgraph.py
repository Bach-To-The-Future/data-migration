"""Which calls close a ring (spec section 6): the artefacts of a project that call each other, directly or through
others, import each other's modules, which Python refuses at run time. A source of the representation says what each
artefact calls; this finds the calls that come back.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping


def closing_calls(calls: Mapping[str, Iterable[str]]) -> dict[str, frozenset[str]]:
    """For each caller, the callees from which the caller can be reached again by calls. Callers with none are left out."""
    graph = {caller: frozenset(callees) for caller, callees in calls.items()}

    def reaches(start: str) -> set[str]:
        seen: set[str] = set()
        pending = [start]
        while pending:
            for callee in graph.get(pending.pop(), ()):
                if callee not in seen:
                    seen.add(callee)
                    pending.append(callee)
        return seen

    reached = {caller: reaches(caller) for caller in graph}
    out = {caller: frozenset(c for c in callees if caller in reached.get(c, ()) or c == caller)
           for caller, callees in graph.items()}
    return {caller: callees for caller, callees in out.items() if callees}
