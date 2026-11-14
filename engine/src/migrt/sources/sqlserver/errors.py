"""Lowering of errors, messages, row counts and identity (spec sections 4 and 5, issue #85).

Statements. `BEGIN TRY ... END TRY BEGIN CATCH ... END CATCH` is `try_catch`: its `body` statements and its `handler`
statements. `RAISERROR` is `raise_error` in the form `raiserror` (children `message`, `severity`, `state` and the
`argument`s the message formats; `options` are LOG, NOWAIT and SETERROR) and `THROW number, message, state` is
`raise_error` in the form `throw`; a bare `THROW;` is `rethrow`. `PRINT` is `emit_message`, with its argument
converted to text where it is not text. A `RAISERROR` that names a message number is kept as written: the messages are
in `sys.messages`, which the catalog does not hold.

Reads. `@@ROWCOUNT` is `rows_affected`, `@@ERROR` is `last_statement_error`, `@@TRANCOUNT` is `transaction_depth`,
`XACT_STATE()` is `transaction_state`, `@@FETCH_STATUS` is `fetch_status`, `ERROR_NUMBER()` and its five siblings are
`error_field` (attribute `field`), and `SCOPE_IDENTITY()`, `@@IDENTITY` and `IDENT_CURRENT('table')` are
`last_identity` (attribute `scope`: `routine`, `session` or `table`, with the `table`). Each is a read of what the
session last held: which statement left the value, and what every statement leaves in `@@ROWCOUNT` and `@@ERROR`, is
the source's `statement_effects`. The error functions may be read outside a handler: a procedure a handler calls
reads the error of its caller. `SET IDENTITY_INSERT table ON|OFF` is `set_session_option`.

A TRY block is not a scope barrier: the scope rules (`core.scopes`) say what may be in scope in the handler and after it."""
from __future__ import annotations

import re

from ...core.ir import Node
from . import names
from . import types as T
from .cursors import Cursors
from .expressions import Opaque, as_role, type_name

GLOBALS = {"@@ROWCOUNT": ("rows_affected", T.INT), "@@ERROR": ("last_statement_error", T.INT),
           "@@TRANCOUNT": ("transaction_depth", T.INT), "@@FETCH_STATUS": ("fetch_status", T.INT)}
IDENTITY = T.decimal(38, 0)
RAISE_OPTIONS = {"Log": "LOG", "NoWait": "NOWAIT", "SetError": "SETERROR"}
MESSAGE_LENGTH, PROCEDURE_LENGTH = 4000, 128


def name_parts(text: str) -> list[str] | None:
    """The parts of a multi-part name written in a string (`sales.[Order]`), None if it is not one."""
    found = re.fullmatch(r"\s*((?:\[[^\]]+\]|[^.\[\]\s]+)(?:\s*\.\s*(?:\[[^\]]+\]|[^.\[\]\s]+))*)\s*", text)
    if found is None:
        return None
    return [a or b for a, b in re.findall(r"\[([^\]]+)\]|([^.\[\]\s]+)", found.group(1))]


class Errors(Cursors):
    # ------------------------------------------------------------------ TRY / CATCH

    def s_TryCatchStatement(self, s) -> list[Node]:
        before = dict(self.temp_tables)
        block = self.statement_list(list(s.TryStatements.Statements), "body")
        after_block = dict(self.temp_tables)
        self.temp_tables = self.merge_temp_tables(before, before, after_block)      # the error may come at any statement
        handler = self.statement_list(list(s.CatchStatements.Statements), "handler")
        self.temp_tables = self.merge_temp_tables(before, after_block, self.temp_tables)
        return [Node("try_catch", self.span(s), None, {}, block + handler)]

    # ------------------------------------------------------------------ RAISERROR, THROW, PRINT

    def s_RaiseErrorStatement(self, s) -> list[Node]:
        first = s.FirstParameter
        if type_name(first) == "IntegerLiteral":
            raise Opaque(s, "RAISERROR with a message number reads the message from sys.messages, which the catalog "
                            "does not hold")
        message = self.lowered_or_opaque(first)
        if message.kind != "opaque_fragment" and (message.type is None or message.type.name != "string"):
            raise Opaque(s, "RAISERROR with a message that is not text (a message number in a variable) reads sys.messages, "
                            "which the catalog does not hold")
        children = [as_role(message, "message"), as_role(self.lowered_or_opaque(s.SecondParameter), "severity"),
                    as_role(self.lowered_or_opaque(s.ThirdParameter), "state")]
        children += [as_role(self.lowered_or_opaque(a), "argument") for a in s.OptionalParameters]
        options = sorted(RAISE_OPTIONS[o.strip()] for o in str(s.RaiseErrorOptions).split(",") if o.strip() in RAISE_OPTIONS)
        return [Node("raise_error", self.span(s), None, {"form": "raiserror", "options": options}, children)]

    def s_ThrowStatement(self, s) -> list[Node]:
        if s.ErrorNumber is None:
            return [Node("rethrow", self.span(s))]
        children = [as_role(self.lowered_or_opaque(s.ErrorNumber), "number"),
                    as_role(self.lowered_or_opaque(s.Message), "message"),
                    as_role(self.lowered_or_opaque(s.State), "state")]
        return [Node("raise_error", self.span(s), None, {"form": "throw", "options": []}, children)]

    def s_PrintStatement(self, s) -> list[Node]:
        value = self.lowered_or_opaque(s.Expression)
        if value.kind not in ("opaque_fragment", "null") and value.type is not None and value.type.name != "string":
            value = Node("implicit_convert", value.span, T.varchar(T.MAX_BYTES, self.default_label), {},
                         [as_role(value, "argument")])
        return [Node("emit_message", self.span(s), None, {}, [as_role(value, "argument")])]

    # ------------------------------------------------------------------ reads of the session's state

    def x_GlobalVariableExpression(self, e) -> Node:
        name = str(e.Name).upper()
        if name == "@@IDENTITY":
            return self.node("last_identity", e, IDENTITY, {"scope": "session"})
        found = GLOBALS.get(name)
        if found is None:
            raise Opaque(e, f"{name} is not lowered yet")
        return self.node(found[0], e, found[1])

    def fn_xact_state(self, e, params) -> Node:
        self.args(e, params, 0)
        return self.node("transaction_state", e, T.integer(16))

    def fn_scope_identity(self, e, params) -> Node:
        self.args(e, params, 0)
        return self.node("last_identity", e, IDENTITY, {"scope": "routine"})

    def fn_ident_current(self, e, params) -> Node:
        if len(params) != 1 or type_name(params[0]) != "StringLiteral":
            raise Opaque(e, "IDENT_CURRENT of a name that is not a string literal is not lowered yet")
        text = str(params[0].Value)
        parts = name_parts(text)
        if parts is None or parts[-1].startswith("#"):
            raise Opaque(e, f"IDENT_CURRENT({text!r}): the name is not a table of the catalog")
        table = self.resolve_table(e, parts, text)
        return self.node("last_identity", e, IDENTITY, {"scope": "table", "table": table})

    def resolve_table(self, fragment, parts: list[str], text: str) -> str:
        try:
            table = self.reader.resolve(tuple(parts), dict(self.name_context, kinds={"table"}))
        except names.AmbiguousName as ambiguous:
            raise Opaque(fragment, str(ambiguous)) from None
        if table is None:
            raise Opaque(fragment, f"{text} does not resolve to a table in the catalog")
        return table

    def read_error_field(self, e, params, field: str, type_) -> Node:
        self.args(e, params, 0)
        return self.node("error_field", e, type_, {"field": field})

    def fn_error_number(self, e, params) -> Node:
        return self.read_error_field(e, params, "number", T.INT)

    def fn_error_severity(self, e, params) -> Node:
        return self.read_error_field(e, params, "severity", T.INT)

    def fn_error_state(self, e, params) -> Node:
        return self.read_error_field(e, params, "state", T.INT)

    def fn_error_line(self, e, params) -> Node:
        return self.read_error_field(e, params, "line", T.INT)

    def fn_error_message(self, e, params) -> Node:
        return self.read_error_field(e, params, "message", T.varchar(MESSAGE_LENGTH, self.default_label, national=True))

    def fn_error_procedure(self, e, params) -> Node:
        return self.read_error_field(e, params, "procedure", T.varchar(PROCEDURE_LENGTH, self.default_label, national=True))

    # ------------------------------------------------------------------ identity insert

    def s_SetIdentityInsertStatement(self, s) -> list[Node]:
        parts = [str(i.Value) for i in s.Table.Identifiers]
        if s.Table.ServerIdentifier is not None or parts[-1].startswith("#"):
            raise Opaque(s, "SET IDENTITY_INSERT on a temp table or through a linked server is not lowered yet")
        table = self.resolve_table(s, parts, ".".join(parts))
        return [Node("set_session_option", self.span(s), None,
                     {"option": "IDENTITY_INSERT", "value": bool(s.IsOn), "table": table})]
