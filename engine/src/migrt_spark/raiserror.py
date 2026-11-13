"""The text of `RAISERROR` and `THROW` (spec section 6, "Errors"), as SQL Server forms it. Each rule here was found by running the
same statement on SQL Server (tests/test_support_errors.py compares them one by one).

`RAISERROR` takes a printf-like text: `%[flags][width][.precision]type` with flags `-`, `+`, space, `0` and `#`, a width or
precision that is a number or `*` (taken from the arguments), and the types `d`, `i`, `u`, `o`, `x`, `X` (a whole number that
fits an `int`) and `s` (text). `%%` is a percent sign. A value that is null, or missing, reads as `(null)`; surplus values are
ignored. A placeholder it does not know is error 2787, and a value of the wrong type is error 2786. A text longer than 2,047
characters is cut to 2,044 and `...`. `THROW` forms its text the same way with no arguments at all: `%%` is a percent sign,
and any other placeholder leaves a single space."""
from __future__ import annotations

import re
from typing import Any

from .errors import Failure

MAX_TEXT = 2047
_SPEC = re.compile(r"%(?P<flags>[-+ 0#]*)(?P<width>\*|\d+)?(?:\.(?P<precision>\*|\d+))?(?P<type>[A-Za-z!])?")
_INTEGER_TYPES = "diuoxX"
_INT_MIN, _INT_MAX = -2**31, 2**31 - 1


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and _INT_MIN <= value <= _INT_MAX


def format_text(template: str, args: tuple) -> str:
    """The text `RAISERROR(template, ..., args)` forms."""
    out: list[str] = []
    taken = 0
    i = 0

    def take() -> tuple[bool, Any, int]:
        nonlocal taken
        taken += 1
        return taken <= len(args), (args[taken - 1] if taken <= len(args) else None), taken

    while i < len(template):
        ch = template[i]
        if ch != "%":
            out.append(ch)
            i += 1
            continue
        if template[i + 1:i + 2] == "%":
            out.append("%")
            i += 2
            continue
        m = _SPEC.match(template, i)
        assert m is not None                                       # the pattern matches a lone percent sign too
        kind = m["type"]
        if kind == "!":                                            # the positional form, which this text does not support
            raise Failure("raise_format_invalid", format=template)
        if kind is None or kind not in _INTEGER_TYPES + "s":
            raise Failure("raise_format_invalid", format=template[i:m.end()])
        i = m.end()
        width = precision = None
        if m["width"] == "*":
            _, w, position = take()
            if w is not None and not _whole(w):
                raise Failure("raise_argument_mismatch", index=position)
            width = w
        elif m["width"]:
            width = int(m["width"])
        if m["precision"] == "*":
            _, p, position = take()
            if p is not None and not _whole(p):
                raise Failure("raise_argument_mismatch", index=position)
            precision = p
        elif m["precision"]:
            precision = int(m["precision"])
        present, value, position = take()
        flags = m["flags"]
        if width is not None and width < 0:
            width, flags = -width, flags + "-"
        if not present or value is None:
            text = "(null)"
            out.append(_pad(text, width, flags.replace("0", "")))
            continue
        if kind == "s":
            if not isinstance(value, str):
                raise Failure("raise_argument_mismatch", index=position)
            text = value if precision is None else value[:max(precision, 0)]
            out.append(_pad(text, width, flags.replace("0", "")))
            continue
        if not _whole(value):
            raise Failure("raise_argument_mismatch", index=position)
        out.append(_pad_number(_number(kind, value, flags, precision), width, flags))
    text = "".join(out)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 3] + "..."


def _number(kind: str, value: int, flags: str, precision: int | None) -> tuple[str, str]:
    """(prefix, digits) of a whole number."""
    if kind in "di":
        digits, prefix = str(abs(value)), "-" if value < 0 else ("+" if "+" in flags else (" " if " " in flags else ""))
    else:
        unsigned = value & 0xFFFFFFFF
        digits = {"u": str(unsigned), "o": format(unsigned, "o"), "x": format(unsigned, "x"), "X": format(unsigned, "X")}[kind]
        prefix = ""
        if "#" in flags and unsigned != 0:
            prefix = {"o": "0", "x": "0x", "X": "0X"}.get(kind, "")
    if precision is not None:
        digits = digits.rjust(precision, "0")
    return prefix, digits


def _pad(text: str, width: int | None, flags: str) -> str:
    if width is None or len(text) >= width:
        return text
    return text.ljust(width) if "-" in flags else text.rjust(width)


def _pad_number(parts: tuple[str, str], width: int | None, flags: str) -> str:
    prefix, digits = parts
    if width is None or len(prefix) + len(digits) >= width:
        return prefix + digits
    if "-" in flags:
        return (prefix + digits).ljust(width)
    if "0" in flags:
        return prefix + digits.rjust(width - len(prefix), "0")
    return (prefix + digits).rjust(width)


def throw_text(message: str) -> str:
    """The text of a `THROW`: its message put through the same formatter with no arguments, where a placeholder other than a
    percent sign leaves a single space."""
    if "%" not in message.replace("%%", ""):
        return message.replace("%%", "%")
    return " "


def clamp_raise_state(state: int) -> int:
    """`RAISERROR`'s state: 0 to 255 as given, larger ones modulo 256, negative ones as 1."""
    return 1 if state < 0 else state % 256
