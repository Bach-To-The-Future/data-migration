"""SQL Server's fixed patterns for likely secrets (spec section 3, "Secrets"): credentials
in connection strings, OPENROWSET, linked-server logins and PASSWORD or SECRET clauses in
T-SQL text, command-line credentials in SQL Agent job steps, and connection details in raw
rows (`provider_string` in sys.servers). Each pattern marks the value, never the key word.
Pattern matching misses some, and the report says so (core.secrets.MISSES_SOME)."""
from __future__ import annotations

import re

from ...core.secrets import SecretSpan, merged

_LITERAL = r"N?'((?:[^']|'')*)'"
# Key=value pairs of a connection string; a value in quotes is T-SQL, caught by the others.
_CONNECTION = re.compile(r"\b(?:user id|uid|password|pwd)\s*=\s*(?!N?')([^\s;'\"][^;'\"\r\n]*)", re.I)
# A variable or named parameter for a credential assigned a literal: @rmtpassword = 'x'.
_VARIABLE = re.compile(r"@\w*(?:password|pwd|secret|rmtuser)\w*\s*=\s*" + _LITERAL, re.I)
# PASSWORD = 'x' and SECRET = 'x': CREATE LOGIN, CREATE CREDENTIAL, keys and certificates.
_CLAUSE = re.compile(r"\b(?:password|secret)\s*=\s*" + _LITERAL, re.I)
# OPENROWSET('provider', 'server'; 'user'; 'password', ...)
_OPENROWSET = re.compile(r"\bOPENROWSET\s*\(\s*" + _LITERAL + r"\s*,\s*" + _LITERAL + r"\s*;\s*" + _LITERAL +
                         r"\s*;\s*" + _LITERAL, re.I)
# sp_addlinkedsrvlogin 'server', 'false', NULL, 'user', 'password', with positional arguments.
_LINKED_LOGIN = re.compile(r"\bsp_addlinkedsrvlogin\b([^;\r\n]*)", re.I)
_ARGUMENT = re.compile(r"\s*(" + _LITERAL + r"|NULL|@\w+\s*=|[^,]+)\s*(?:,|$)", re.I)
# Command-line credentials: sqlcmd and bcp -U and -P, dtexec /DE[CRYPT].
_COMMAND_LINE = [re.compile(r"(?<!\S)-[UP]\s*([^\s\"]+)"), re.compile(r"(?<!\S)/DE(?:CRYPT)?\s+([^\s\"]+)", re.I)]
# Raw columns that are connection details whenever they hold a value.
CONNECTION_COLUMNS = {("sys.servers", "provider_string")}


def _groups(pattern: re.Pattern, text: str, name: str, groups=None) -> list[SecretSpan]:
    spans = []
    for match in pattern.finditer(text):
        for group in groups or [len(match.groups())]:
            value = match.group(group)
            if value is None:
                continue
            start, value = match.start(group), value.rstrip()
            if value:
                spans.append(SecretSpan(start, len(value), name))
    return spans


def _linked_logins(text: str) -> list[SecretSpan]:
    spans = []
    for match in _LINKED_LOGIN.finditer(text):
        args, at = match.group(1), match.start(1)
        position, index = 0, 0
        while position < len(args):
            arg = _ARGUMENT.match(args, position)
            if arg is None or arg.end() == position:
                break
            if arg.group(1).startswith("@"):
                break   # named arguments: _VARIABLE marks them
            if index in (3, 4) and arg.group(2) is not None and arg.group(2):
                spans.append(SecretSpan(at + arg.start(2), len(arg.group(2)), "linked-server login"))
            position, index = arg.end(), index + 1
    return spans


def in_text(text: str, *, command_line: bool = False) -> list[SecretSpan]:
    """Mark likely secrets in T-SQL text, or in a job step's command when command_line."""
    if not text:
        return []
    spans = (_groups(_CONNECTION, text, "connection string") + _groups(_VARIABLE, text, "credential variable") +
             _groups(_CLAUSE, text, "password or secret clause") +
             _groups(_OPENROWSET, text, "OPENROWSET credentials", groups=[3, 4]) + _linked_logins(text))
    if command_line:
        for pattern in _COMMAND_LINE:
            spans += _groups(pattern, text, "command-line credentials")
    return merged(spans)


# An SSIS package's XML: a connection manager's connection string, as an attribute or as an
# element, and the value of every property the package marks sensitive, such as a password.
_PACKAGE_CONNECTION = re.compile(r"\b(?:\w+:)?ConnectionString=\"([^\"]*)\"")
_PACKAGE_CONNECTION_ELEMENT = re.compile(r"<(?:\w+:)?Property\s[^>]*?(?:\w+:)?Name=\"ConnectionString\"[^>]*>([^<]*)<",
                                         re.I)
_PACKAGE_SENSITIVE = re.compile(r"<[\w:]+\s[^>]*?(?:\w+:)?Sensitive=\"1\"[^>]*>([^<]*)<", re.I)
_PACKAGE_PASSWORD = re.compile(r"<(?:\w+:)?Property\s[^>]*?(?:\w+:)?Name=\"Password\"[^>]*>([^<]*)<", re.I)


def whole(value: str | None, pattern: str) -> list[SecretSpan]:
    """A value that is a likely secret through and through, such as a connection string."""
    return [SecretSpan(0, len(value), pattern)] if value else []


def in_package(text: str) -> list[SecretSpan]:
    """Mark likely secrets in an SSIS package's XML, as the file holds it: connection strings
    whole, every sensitive property's value, and the T-SQL patterns over the whole text."""
    spans = (_groups(_PACKAGE_CONNECTION, text, "connection string") +
             _groups(_PACKAGE_CONNECTION_ELEMENT, text, "connection string") +
             _groups(_PACKAGE_SENSITIVE, text, "sensitive property") +
             _groups(_PACKAGE_PASSWORD, text, "sensitive property"))
    return merged(spans + in_text(text))


def in_rows(view: str, rows: list[dict], *, command_columns: frozenset[str] = frozenset()) -> list[SecretSpan]:
    """Mark likely secrets in the raw rows of one metadata view: every string cell is scanned
    with the text patterns, and connection-detail columns are marked whole."""
    spans = []
    for index, row in enumerate(rows):
        for column, value in row.items():
            if not isinstance(value, str) or not value:
                continue
            if (view, column) in CONNECTION_COLUMNS:
                spans.append(SecretSpan(0, len(value), "connection details", index, column))
                continue
            for span in in_text(value, command_line=column in command_columns):
                spans.append(SecretSpan(span.start, span.length, span.pattern, index, column))
    return merged(spans)
