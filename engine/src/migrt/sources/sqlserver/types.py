"""The SQL Server half of the type mapping (spec section 6, ADR 0019): declared types to
vocabulary types, and the T-SQL typing rules lowering needs for the operations it handles."""
from __future__ import annotations

from ...core.ir import UNKNOWN, VType

INTEGER_BITS = {"tinyint": 8, "smallint": 16, "int": 32, "bigint": 64}


def integer(bits: int) -> VType:
    return VType.of("integer", bits=bits, signed=bits != 8)


INT = integer(32)


def from_column(column: dict) -> VType:
    """A catalog column's declared type as a vocabulary type."""
    name = column["system_type"]
    if name in INTEGER_BITS:
        return integer(INTEGER_BITS[name])
    if name == "bit":
        return VType.of("boolean")
    if name in ("decimal", "numeric"):
        return VType.of("decimal", precision=column["precision"], scale=column["scale"])
    if name == "money":
        return VType.of("money", precision=19)
    if name == "smallmoney":
        return VType.of("money", precision=10)
    if name == "float":
        return VType.of("float", bits=53)
    if name == "real":
        return VType.of("float", bits=24)
    if name == "date":
        return VType.of("date")
    if name == "datetime":
        return VType.of("local_timestamp", rounding="1/300 second")
    if name == "smalldatetime":
        return VType.of("local_timestamp", rounding="minute")
    if name == "datetime2":
        return VType.of("local_timestamp", rounding=f"{column['scale']} digits")
    if name in ("char", "varchar", "nchar", "nvarchar"):
        unit = "utf16_units" if name.startswith("n") else "bytes"
        length = column["max_length"]
        if length == -1:
            length = "unlimited"
        elif unit == "utf16_units":
            length //= 2
        return VType.of("string", length=length, unit=unit, fixed=name in ("char", "nchar"),
                        collation=column["collation"])
    if name in ("sql_variant", "hierarchyid", "geography", "geometry"):
        return VType.of("unsupported_type", source_name=name)
    return UNKNOWN


def is_integer(t: VType | None) -> bool:
    return t is not None and t.name == "integer"


def is_money(t: VType | None) -> bool:
    return t is not None and t.name == "money"


def wider_integer(a: VType, b: VType) -> VType:
    """T-SQL data type precedence among integers: the result takes the wider one."""
    return a if a.int_param("bits") >= b.int_param("bits") else b


def sum_type(t: VType) -> VType | None:
    """SUM and AVG over integers return int, or bigint over bigint; over money, money."""
    if is_integer(t):
        return integer(64) if t.param("bits") == 64 else INT
    if is_money(t):
        return VType.of("money", precision=19)
    return None
