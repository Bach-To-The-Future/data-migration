"""Name adjustments (spec section 3): how a source name becomes a name the target can take,
recorded once and only ever extended.

A target supplies the rules (which characters it refuses, how it compares names, which
names are reserved); the core applies them the same way for every target. Within one scope,
each refused character becomes `_`; names that then collide, by the rule's comparison, keep
the first in sorted order and give the others a numeric suffix (spec section 3). So a name
that needed no change can still get a suffix, when a name sorted before it was adjusted
into it. Every name is recorded, changed or not, so a later name can never take one; a
recorded name never changes, so adding a name never renames an existing one. The control
database keeps the record (control_db.name_adjustments).
"""
from __future__ import annotations

import dataclasses
from typing import Callable, Iterable


@dataclasses.dataclass(frozen=True)
class Rule:
    name: str                                  # recorded with every adjustment
    adjust: Callable[[str], str]               # replace what the target refuses
    fold: Callable[[str], str]                 # names equal after folding collide
    reserved: Callable[[str], bool] = lambda _name: False
    max_length: int | None = None


def extend(rule: Rule, names: Iterable[str], recorded: dict[str, str]) -> dict[str, str]:
    """The adjustments for the names in one scope that are not recorded yet: source name ->
    target name. `recorded` is what the scope already holds, and is kept as it is."""
    taken = {rule.fold(target) for target in recorded.values()}
    new: dict[str, str] = {}
    for name in sorted(set(names) - set(recorded)):
        base = rule.adjust(name)
        candidate, n = base, 2
        while rule.fold(candidate) in taken or rule.reserved(candidate):
            candidate, n = f"{base}_{n}", n + 1
        if rule.max_length is not None and len(candidate) > rule.max_length:
            raise ValueError(f"{name!r} becomes {candidate!r}, longer than the {rule.max_length} characters "
                             f"{rule.name} takes")
        taken.add(rule.fold(candidate))
        new[name] = candidate
    return new
