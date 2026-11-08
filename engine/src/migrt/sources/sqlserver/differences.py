"""The SQL Server source half of the silent-differences catalogue (Annex C; ADR 0019), as
data: how each SQL Server construct behaves, in vocabulary terms. Lowering and the test-data
generator's edge-case pools read it through the core's catalogue, never this file."""
from __future__ import annotations

import functools
import json
import pathlib

from ...core.differences import SourceHalf

PATH = pathlib.Path(__file__).with_name("differences.json")


@functools.cache
def data() -> dict:
    return json.loads(PATH.read_text(encoding="utf-8"))


@functools.cache
def half() -> SourceHalf:
    return SourceHalf.from_data(data())
