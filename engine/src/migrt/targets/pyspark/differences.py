"""The PySpark target half of the silent-differences catalogue (Annex D; ADR 0019), as data:
where Spark's built-ins differ from the construct's meaning, and what reproduces it. The
support library, tolerances and mutation read it through the core's catalogue."""
from __future__ import annotations

import functools
import json
import pathlib

from ...core.differences import TargetHalf

PATH = pathlib.Path(__file__).with_name("differences.json")


@functools.cache
def data() -> dict:
    return json.loads(PATH.read_text(encoding="utf-8"))


@functools.cache
def half() -> TargetHalf:
    return TargetHalf.from_data(data())
