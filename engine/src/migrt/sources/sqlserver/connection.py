"""A SQL Server connection through Microsoft.Data.SqlClient (spec section 18), with
FireInfoMessageEventOnUserErrors on so every error arrives as a message and later results
are not lost. Because errors then no longer raise by themselves, this wrapper raises
SqlServerError after any statement that produced one: a failed read never passes silently."""
from __future__ import annotations

import dataclasses
import decimal

from . import dotnet


@dataclasses.dataclass(frozen=True)
class ServerMessage:
    number: int
    severity: int
    state: int
    text: str


class SqlServerError(Exception):
    def __init__(self, errors: list[ServerMessage]):
        self.errors = errors
        super().__init__("; ".join(f"error {e.number} (severity {e.severity}): {e.text}" for e in errors))

    @property
    def numbers(self) -> set[int]:
        return {e.number for e in self.errors}


def _to_python(value):
    import System  # pyright: ignore[reportMissingImports]  (a .NET namespace, loaded by pythonnet at run time)

    if value is None or isinstance(value, System.DBNull):
        return None
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, System.Decimal):
        return decimal.Decimal(value.ToString(System.Globalization.CultureInfo.InvariantCulture))
    if isinstance(value, System.DateTime):
        return value.ToString("yyyy-MM-ddTHH:mm:ss.fffffff", System.Globalization.CultureInfo.InvariantCulture)
    if isinstance(value, System.Guid):
        return value.ToString()
    if isinstance(value, System.Array[System.Byte]):
        return bytes(value)
    return str(value)


class Connection:
    """One open connection. Use as a context manager."""

    def __init__(self, host: str, port: int, login: str, password: str, database: str = "master",
                 application: str = "migrt"):
        dotnet.load()
        from Microsoft.Data.SqlClient import SqlConnection, SqlConnectionStringBuilder  # pyright: ignore[reportMissingImports]  (a .NET namespace, loaded by pythonnet at run time)

        builder = SqlConnectionStringBuilder()
        builder.DataSource = f"{host},{port}"
        builder.InitialCatalog = database
        builder.UserID = login
        builder.Password = password
        builder.TrustServerCertificate = True
        builder.ApplicationName = application
        builder.ConnectTimeout = 15
        # No pooling: a closed connection logs out, so the tool leaves no session behind.
        builder.Pooling = False
        self._conn = SqlConnection(builder.ConnectionString)
        self._conn.FireInfoMessageEventOnUserErrors = True
        self._messages: list[ServerMessage] = []
        self._conn.InfoMessage += self._on_message
        try:
            self._conn.Open()
        except Exception as exc:  # a .NET SqlException: login failed, server unreachable
            raise SqlServerError([_from_exception(exc)]) from None

    def _on_message(self, _sender, args):
        for error in args.Errors:
            self._messages.append(ServerMessage(int(error.Number), int(error.Class), int(error.State), str(error.Message)))

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        """Run one batch and return the rows of its first result set, as dicts by column name.
        Raises SqlServerError if any statement in it raised an error."""
        from Microsoft.Data.SqlClient import SqlCommand  # pyright: ignore[reportMissingImports]  (a .NET namespace, loaded by pythonnet at run time)

        self._messages.clear()
        command = SqlCommand(sql, self._conn)
        command.CommandTimeout = 120
        for name, value in (params or {}).items():
            command.Parameters.AddWithValue("@" + name, value)
        rows: list[dict] = []
        try:
            reader = command.ExecuteReader()
        except Exception as exc:
            raise SqlServerError(self._errors() or [_from_exception(exc)]) from None
        try:
            first = True
            while True:
                if first and reader.FieldCount:
                    names = [reader.GetName(i) for i in range(reader.FieldCount)]
                    while reader.Read():
                        rows.append({n: _to_python(reader.GetValue(i)) for i, n in enumerate(names)})
                    first = False
                if not reader.NextResult():
                    break
        finally:
            reader.Close()
        errors = self._errors()
        if errors:
            raise SqlServerError(errors)
        return rows

    def _errors(self) -> list[ServerMessage]:
        return [m for m in self._messages if m.severity > 10]

    def close(self) -> None:
        self._conn.Close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _from_exception(exc) -> ServerMessage:
    number = getattr(exc, "Number", 0)
    severity = getattr(exc, "Class", 20)
    return ServerMessage(int(number or 0), int(severity or 20), 0, str(getattr(exc, "Message", exc)))
