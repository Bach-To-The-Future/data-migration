"""Twins: T-SQL's rules for single values, for procedural code (spec section 6, "Procedural code and twins").

A variable of generated procedural code is a Python value, and `None` is NULL. A comparison with NULL is unknown in
T-SQL and an ordinary `False` or an error in Python; `AND` and `OR` are three-valued; arithmetic on integers errors on
overflow where Python grows. Each twin takes and returns Python values and does what SQL Server does to the same single
values, errors included (`ss.fail`, so the error number is set for `@@ERROR`). Generated code reaches them as `ss.twin`.

A truth value is `True`, `False` or `None` (unknown). `holds` is what `IF` and `WHILE` do with one: only true goes in.
SQL Server's tests of these twins are in `tests/test_support_twins.py`; they compare each with SQL Server, never with a
table written here."""
from __future__ import annotations

import operator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .session import SessionState

COMPARISONS = {"=": operator.eq, "<>": operator.ne, ">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le}
ARITHMETIC = {"add": operator.add, "subtract": operator.sub, "multiply": operator.mul}


class Twins:
    def __init__(self, ss: "SessionState"):
        self._ss = ss

    # ------------------------------------------------------------------ three-valued logic

    @staticmethod
    def compare(op: str, a: Any, b: Any) -> bool | None:
        """`a op b`; unknown if either is NULL."""
        if a is None or b is None:
            return None
        return COMPARISONS[op](a, b)

    @staticmethod
    def and_(a: bool | None, b: bool | None) -> bool | None:
        if a is False or b is False:
            return False
        return None if a is None or b is None else True

    @staticmethod
    def or_(a: bool | None, b: bool | None) -> bool | None:
        if a is True or b is True:
            return True
        return None if a is None or b is None else False

    @staticmethod
    def not_(a: bool | None) -> bool | None:
        return None if a is None else not a

    @staticmethod
    def is_null(value: Any) -> bool:
        return value is None

    @staticmethod
    def holds(truth: bool | None) -> bool:
        """What `IF` and `WHILE` run on: a condition that is false or unknown does not."""
        return truth is True

    @staticmethod
    def first_non_null(*items: Any) -> Any:
        """`COALESCE`: the first argument that is not NULL, NULL if there is none. An argument given as a function is evaluated
        only if the ones before it were NULL, as SQL Server evaluates it (COALESCE is a CASE): `COALESCE(1, 1 / 0)` is 1."""
        for item in items:
            value = item() if callable(item) else item
            if value is not None:
                return value
        return None

    @classmethod
    def between(cls, value: Any, low: Any, high: Any) -> bool | None:
        """`value BETWEEN low AND high`: `value >= low AND value <= high`, three-valued."""
        return cls.and_(cls.compare(">=", value, low), cls.compare("<=", value, high))

    @classmethod
    def in_list(cls, value: Any, *items: Any) -> bool | None:
        """`value IN (items)`: `value = item1 OR value = item2 ...`, so a NULL value, or a NULL item when nothing matches,
        makes it unknown."""
        result: bool | None = False
        for item in items:
            result = cls.or_(result, cls.compare("=", value, item))
        return result

    # ------------------------------------------------------------------ integers

    def to_integer(self, value: int | None, *, bits: int, signed: bool) -> int | None:
        """A value stored in an integer of `bits` bits: NULL stays NULL, a value outside the type's range is an overflow."""
        if value is None:
            return None
        low, high = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
        if not low <= value <= high:
            raise self._ss.fail("arithmetic_overflow", to_type=f"integer({bits}, {'signed' if signed else 'unsigned'})")
        return value

    def negate(self, a: int | None, *, bits: int, signed: bool) -> int | None:
        """The number with its sign changed; the one value whose negation does not fit (the smallest) is an overflow."""
        return None if a is None else self.to_integer(-a, bits=bits, signed=signed)

    def add(self, a: int | None, b: int | None, *, bits: int, signed: bool) -> int | None:
        return self._arithmetic("add", a, b, bits, signed)

    def subtract(self, a: int | None, b: int | None, *, bits: int, signed: bool) -> int | None:
        return self._arithmetic("subtract", a, b, bits, signed)

    def multiply(self, a: int | None, b: int | None, *, bits: int, signed: bool) -> int | None:
        return self._arithmetic("multiply", a, b, bits, signed)

    def integer_divide(self, a: int | None, b: int | None, *, bits: int, signed: bool) -> int | None:
        """Integer division truncates toward zero. A zero divisor is an error, and so is the one quotient that does not fit
        (the smallest value divided by -1)."""
        if a is None or b is None:
            return None
        if b == 0:
            raise self._ss.fail("divide_by_zero")
        quotient = abs(a) // abs(b)
        return self.to_integer(-quotient if (a < 0) != (b < 0) else quotient, bits=bits, signed=signed)

    def modulo(self, a: int | None, b: int | None, *, bits: int, signed: bool) -> int | None:
        """The remainder has the sign of the dividend. A zero divisor is an error, and so is the smallest value of the type
        divided by -1, which SQL Server computes as the quotient and so overflows (decided against SQL Server)."""
        if a is None or b is None:
            return None
        if b == 0:
            raise self._ss.fail("divide_by_zero")
        if signed and b == -1 and a == -(1 << (bits - 1)):
            raise self._ss.fail("arithmetic_overflow", to_type=f"integer({bits}, signed)")
        remainder = abs(a) % abs(b)
        return self.to_integer(-remainder if a < 0 else remainder, bits=bits, signed=signed)

    def _arithmetic(self, name: str, a: int | None, b: int | None, bits: int, signed: bool) -> int | None:
        if a is None or b is None:
            return None
        return self.to_integer(ARITHMETIC[name](a, b), bits=bits, signed=signed)
