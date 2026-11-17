"""What a generated procedure takes and gives back (spec section 6, "What each artefact type becomes").

A procedure is a function whose parameters are keyword arguments and whose one result is a `ProcedureResult`: the return
code, the output parameters by name and the result sets as an ordered list of DataFrames.

`DEFAULT` is what a caller passes for a parameter to get its default, as T-SQL's `DEFAULT` does; leaving the argument out
does the same. It is `Ellipsis`: a value no T-SQL value can be, written `...`, so generated code needs no import for it."""
from __future__ import annotations

import dataclasses
from typing import Any

from pyspark.sql import DataFrame

DEFAULT = Ellipsis


@dataclasses.dataclass
class ProcedureResult:
    return_code: int
    outputs: dict[str, Any]
    result_sets: list[DataFrame]
