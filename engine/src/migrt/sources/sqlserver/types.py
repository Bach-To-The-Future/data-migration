"""The SQL Server half of the type mapping (spec section 6, ADR 0019): declared types to
vocabulary types, and the T-SQL typing rules lowering needs: data type precedence, the
precision and scale of decimal arithmetic, and the types of aggregates."""
from __future__ import annotations

from ...core.ir import UNKNOWN, VType
from . import collations

INTEGER_BITS = {"tinyint": 8, "smallint": 16, "int": 32, "bigint": 64}
UNLIMITED = "unlimited"
MAX_BYTES, MAX_UTF16 = 8000, 4000
# The decimal an integer or money type converts to when it meets a decimal (SQL Server's documentation of
# arithmetic: int is treated as decimal(10,0), money as decimal(19,4)).
INTEGER_AS_DECIMAL = {8: (3, 0), 16: (5, 0), 32: (10, 0), 64: (19, 0)}


def integer(bits: int) -> VType:
    return VType.of("integer", bits=bits, signed=bits != 8)


INT = integer(32)
BIGINT = integer(64)
BOOLEAN = VType.of("boolean")
FLOAT = VType.of("float", bits=53)


def decimal(precision: int, scale: int) -> VType:
    return VType.of("decimal", precision=precision, scale=scale)


def money(precision: int = 19) -> VType:
    return VType.of("money", precision=precision)


def string(length, *, unit: str, fixed: bool, label: collations.Label | None) -> VType:
    """A string type; `label` is its collation and the origin of that collation. A string whose
    collation the catalog does not hold has none, and no comparison of it can name a rule."""
    return VType.of("string", length=length, unit=unit, fixed=fixed,
                    collation=label.collation if label else None, origin=label.origin if label else None)


def varchar(length, label: collations.Label | None, national: bool = False, fixed: bool = False) -> VType:
    return string(length, unit="utf16_units" if national else "bytes", fixed=fixed, label=label)


def label_of(t: VType) -> collations.Label | None:
    collation = t.param("collation")
    return collations.Label(collation, t.param("origin") or collations.DEFAULT) if collation else None


def text_like(t: VType) -> VType:
    """A varchar or nvarchar of the length, collation and kind of the string `t`: what the string functions return."""
    return varchar(t.length(), label_of(t), t.param("unit") == "utf16_units")


def relabelled(t: VType, label: collations.Label | None) -> VType:
    return string(t.length(), unit=t.str_param("unit"), fixed=t.flag("fixed"), label=label)


def from_column(column: dict, *, default: collations.Label | None = None) -> VType:
    """A catalog column's, parameter's or function result's declared type as a vocabulary type.
    A string has its column's collation (implicit), or `default`, the database's collation
    (coercible-default), where the thing has none of its own, as a variable has not."""
    name = column["system_type"]
    if name in INTEGER_BITS:
        return integer(INTEGER_BITS[name])
    if name == "bit":
        return BOOLEAN
    if name in ("decimal", "numeric"):
        return decimal(column["precision"], column["scale"])
    if name == "money":
        return money(19)
    if name == "smallmoney":
        return money(10)
    if name == "float":
        return FLOAT
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
    if name == "datetimeoffset":
        return VType.of("timestamp_with_offset", digits=column["scale"])
    if name == "time":
        return VType.of("time_of_day", digits=column["scale"])
    if name in ("char", "varchar", "nchar", "nvarchar", "sysname", "text", "ntext"):
        national = name.startswith("n") or name == "sysname"
        length = column["max_length"]
        if name in ("text", "ntext") or length == -1:
            length = UNLIMITED
        elif national:
            length //= 2
        label = collations.Label(column["collation"], collations.COLUMN) if column.get("collation") else default
        return varchar(length, label, national, fixed=name in ("char", "nchar"))
    if name in ("binary", "varbinary", "image", "timestamp", "rowversion"):
        length = UNLIMITED if name == "image" or column["max_length"] == -1 else column["max_length"]
        return VType.of("binary", length=length, fixed=name in ("binary", "timestamp", "rowversion"))
    if name == "uniqueidentifier":
        return VType.of("guid")
    if name == "xml":
        return VType.of("xml")
    if name in ("sql_variant", "hierarchyid", "geography", "geometry", "clr_type", "table"):
        return VType.of("unsupported_type", source_name=name)
    return UNKNOWN


def declared(name: str, arguments: list, default: collations.Label | None, *, converting: bool = False) -> VType:
    """The type a type reference in T-SQL names. `arguments` are its parenthesised numbers, `"max"` for MAX.
    A string with no length is 1 in a declaration and 30 in CAST or CONVERT (`converting`)."""
    name = name.lower()
    first = arguments[0] if arguments else None
    second = arguments[1] if len(arguments) > 1 else None
    if name in ("char", "varchar", "nchar", "nvarchar", "binary", "varbinary"):
        default_length = 30 if converting else 1
        length = default_length if first is None else (UNLIMITED if first == "max" else int(first))
        bytes_per = 2 if name.startswith("n") else 1
        max_length = -1 if length == UNLIMITED else int(length) * bytes_per
        return from_column({"system_type": name, "max_length": max_length, "precision": 0, "scale": 0,
                            "collation": None}, default=default)
    shape = {"max_length": 0, "precision": 0, "scale": 0, "collation": None}
    if name in ("decimal", "numeric", "dec"):
        name, shape["precision"], shape["scale"] = "decimal", int(first) if first is not None else 18, int(second or 0)
    elif name == "float":
        name = "real" if first is not None and int(first) <= 24 else "float"
    elif name in ("datetime2", "datetimeoffset", "time"):
        shape["scale"] = 7 if first is None else int(first)
    elif name in ("integer",):
        name = "int"
    return from_column({"system_type": name, **shape}, default=default)


# ---------------------------------------------------------------- data type precedence

def precedence(t: VType) -> int:
    """SQL Server's data type precedence: the lower the number, the higher the precedence. A value of a
    lower-precedence type is converted to the higher one when two meet."""
    n = t.name
    if n == "xml":
        return 3
    if n == "timestamp_with_offset":
        return 4
    if n == "local_timestamp":
        rounding = str(t.param("rounding"))
        return 6 if rounding == "1/300 second" else 7 if rounding == "minute" else 5
    if n == "date":
        return 8
    if n == "time_of_day":
        return 9
    if n == "float":
        return 10 if t.param("bits") == 53 else 11
    if n == "decimal":
        return 12
    if n == "money":
        return 13 if t.param("precision") == 19 else 14
    if n == "integer":
        return {64: 15, 32: 16, 16: 17, 8: 18}[t.int_param("bits")]
    if n == "boolean":
        return 19
    if n == "guid":
        return 24
    if n == "string":
        national = t.param("unit") == "utf16_units"
        return (26 if t.param("fixed") else 25) if national else (28 if t.param("fixed") else 27)
    if n == "binary":
        return 30 if t.param("fixed") else 29
    return 1


def is_integer(t: VType | None) -> bool:
    return t is not None and t.name == "integer"


def is_money(t: VType | None) -> bool:
    return t is not None and t.name == "money"


def is_string(t: VType | None) -> bool:
    return t is not None and t.name == "string"


def is_exact_numeric(t: VType | None) -> bool:
    return t is not None and t.name in ("integer", "decimal", "money")


def is_numeric(t: VType | None) -> bool:
    return is_exact_numeric(t) or (t is not None and t.name == "float")


def is_temporal(t: VType | None) -> bool:
    return t is not None and t.name in ("date", "local_timestamp", "timestamp_with_offset", "time_of_day")


def wider_integer(a: VType, b: VType) -> VType:
    """T-SQL data type precedence among integers: the result takes the wider one."""
    return a if a.int_param("bits") >= b.int_param("bits") else b


def needs_conversion(source: VType, target: VType) -> bool:
    """Whether a value of `source` meeting `target` is converted by SQL Server: a different family, a different
    integer width, a different string unit, or a different date-time type. Precision, scale and length differences
    inside one family are not conversions."""
    if source.name != target.name:
        return True
    if source.name == "integer":
        return source.int_param("bits") != target.int_param("bits")
    if source.name == "string":
        return source.param("unit") != target.param("unit")
    if source.name == "float":
        return source.param("bits") != target.param("bits")
    if source.name == "local_timestamp":
        return precedence(source) != precedence(target)
    return False


def higher(a: VType, b: VType) -> VType:
    return a if precedence(a) <= precedence(b) else b


def unified(a: VType, b: VType) -> VType:
    """The type of a value that may come from either of two expressions of the same family: the longer string, the
    decimal wide enough for both, the wider integer."""
    if a.name != b.name:
        top = higher(a, b)
        if top.name == "decimal":                  # an integer or money meeting a decimal is the decimal it converts to
            da, db = as_decimal(a), as_decimal(b)
            if da is not None and db is not None:
                return unified(da, db)
        return top
    if a.name == "string":
        la, lb = a.length(), b.length()
        length = UNLIMITED if isinstance(la, str) or isinstance(lb, str) else max(la, lb)
        stronger = a if collations.STRENGTH.get(str(a.param("origin")), 0) >= collations.STRENGTH.get(
            str(b.param("origin")), 0) else b
        return string(length, unit=a.str_param("unit"), fixed=a.flag("fixed") and b.flag("fixed"),
                      label=label_of(stronger))
    if a.name == "decimal":
        scale = max(a.int_param("scale"), b.int_param("scale"))
        integral = max(a.int_param("precision") - a.int_param("scale"), b.int_param("precision") - b.int_param("scale"))
        return decimal(min(38, integral + scale), scale)
    if a.name == "integer":
        return wider_integer(a, b)
    if a.name == "money":
        return a if a.int_param("precision") >= b.int_param("precision") else b
    if a.name == "float":
        return a if a.int_param("bits") >= b.int_param("bits") else b
    return a


def as_decimal(t: VType) -> VType | None:
    """The decimal an exact numeric meets a decimal as."""
    if t.name == "decimal":
        return t
    if t.name == "integer":
        return decimal(*INTEGER_AS_DECIMAL[t.int_param("bits")])
    if t.name == "money":
        return decimal(t.int_param("precision"), 4)
    return None


def _shrunk(precision: int, scale: int) -> VType:
    """A decimal result is capped at precision 38, giving up scale (never below 6) before integral digits."""
    if precision <= 38:
        return decimal(precision, scale)
    return decimal(38, max(38 - (precision - scale), min(scale, 6)))


def decimal_result(op: str, a: VType, b: VType) -> VType:
    """The precision and scale of decimal arithmetic, from SQL Server's documentation."""
    p1, s1, p2, s2 = a.int_param("precision"), a.int_param("scale"), b.int_param("precision"), b.int_param("scale")
    if op in ("add", "subtract"):
        return _shrunk(max(s1, s2) + max(p1 - s1, p2 - s2) + 1, max(s1, s2))
    if op == "multiply":
        return _shrunk(p1 + p2 + 1, s1 + s2)
    if op == "divide":
        scale = max(6, s1 + p2 + 1)
        return _shrunk(p1 - s1 + s2 + scale, scale)
    if op == "modulo":
        return _shrunk(min(p1 - s1, p2 - s2) + max(s1, s2), max(s1, s2))
    raise ValueError(op)


def arithmetic(op: str, a: VType, b: VType) -> tuple[str, VType] | None:
    """The operation and result type of `op` (add, subtract, multiply, divide, modulo) on two operands that are
    already of one family (the caller converts the lower-precedence one). None where SQL Server refuses the
    operation or lowering does not handle it."""
    family = a.name
    if family == "integer":
        result = wider_integer(a, b)
        return ({"divide": "integer_divide"}.get(op, op), result)
    if family == "decimal":
        return ({"divide": "decimal_divide"}.get(op, op), decimal_result(op, a, b))
    if family == "money":
        result = unified(a, b)
        if op == "modulo":
            return None
        return ({"multiply": "money_multiply", "divide": "money_divide"}.get(op, op), result)
    if family == "float":
        if op == "modulo":
            return None
        return ({"divide": "float_divide"}.get(op, op), unified(a, b))
    return None


def concatenated(a: VType, b: VType) -> VType:
    """The type of `a + b` for two strings: the higher-precedence kind, the lengths added (capped at the largest a
    non-MAX string holds), the collation of precedence's winner. Two different implicit collations raise CollationConflict."""
    top = higher(a, b)
    la, lb = a.length(), b.length()
    national = top.param("unit") == "utf16_units"
    cap = MAX_UTF16 if national else MAX_BYTES
    length = UNLIMITED if isinstance(la, str) or isinstance(lb, str) else min(la + lb, cap)
    first, second = label_of(a), label_of(b)
    label = collations.combine(first, second) if first and second else None    # a conflict is raised
    return string(length, unit=top.str_param("unit"), fixed=False, label=label)


def sum_type(t: VType) -> VType | None:
    """SUM's result: int for tinyint, smallint and int; bigint for bigint; decimal(38, s) for a decimal; money for
    money; float for float."""
    if is_integer(t):
        return BIGINT if t.int_param("bits") == 64 else INT
    if t.name == "decimal":
        return decimal(38, t.int_param("scale"))
    if t.name == "money":
        return money(19)            # smallmoney totals are money
    if t.name == "float":
        return FLOAT
    return None


def average_type(t: VType) -> VType | None:
    """AVG's result: the same integer rule as SUM (truncated), decimal(38, max(s, 6)) for a decimal."""
    if t.name == "decimal":
        return decimal(38, max(t.int_param("scale"), 6))
    return sum_type(t)
