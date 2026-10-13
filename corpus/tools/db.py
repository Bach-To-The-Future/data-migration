"""SQL Server access for the corpus tools. Runs inside the sandbox tools container."""
import os
import re
import time

import pymssql

BACKUP_DIR = "/var/opt/mssql/backup"
DATA_DIR = "/var/opt/mssql/data"

_GO = re.compile(r"^[ \t]*GO[ \t]*(?:--[^\n]*)?$", re.IGNORECASE | re.MULTILINE)


def connect(database="master"):
    return pymssql.connect(
        server=os.environ.get("MSSQL_HOST", "sqlserver"),
        user="sa",
        password=os.environ["MSSQL_SA_PASSWORD"],
        database=database,
        autocommit=True,
        tds_version="7.4",
        login_timeout=10,
    )


def wait_for_server(timeout=180):
    deadline = time.monotonic() + timeout
    while True:
        try:
            conn = connect()
            conn.close()
            return
        except pymssql.Error:
            if time.monotonic() > deadline:
                raise
            time.sleep(3)


def split_batches(script):
    """Split a script on GO separators, as sqlcmd and Management Studio do."""
    return [b for b in (part.strip() for part in _GO.split(script)) if b]


def execute(conn, sql):
    """Run one batch and discard its result sets."""
    cur = conn.cursor()
    cur.execute(sql)
    while cur.nextset():
        pass
    cur.close()


def query(conn, sql, params=None):
    """Run one batch and return the rows of its first result set."""
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall() if cur.description else []
    cur.close()
    return rows


def query_all(conn, sql):
    """Run one batch and return every result set as (columns, rows)."""
    cur = conn.cursor()
    cur.execute(sql)
    sets = []
    while True:
        if cur.description:
            sets.append(([c[0] for c in cur.description], cur.fetchall()))
        if not cur.nextset():
            break
    cur.close()
    return sets


def run_script(conn, text):
    for batch in split_batches(text):
        execute(conn, batch)


def observe(database, sql):
    """Run one batch to its end even when a statement in it fails.

    Errors below severity 20 are collected as messages and do not stop the
    batch: a two-second WAITFOR placed after a failing statement still ran
    in full, and the statement after it took effect. The driver does drop
    the result sets of a batch in which a statement failed, so read the
    resulting state back in a separate batch.
    Returns (result_sets, messages), where a result set is (columns, rows)
    and a message is (severity, text).
    """
    from pymssql import _mssql

    messages = []

    def on_message(state, severity, server, procedure, line, text):
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        messages.append((severity, text.strip()))

    previous = _mssql.min_error_severity
    _mssql.min_error_severity = 20
    conn = None
    try:
        conn = _mssql.connect(
            server=os.environ.get("MSSQL_HOST", "sqlserver"),
            user="sa",
            password=os.environ["MSSQL_SA_PASSWORD"],
            database=database,
            tds_version="7.4",
        )
        conn.set_msghandler(on_message)
        sets = []
        try:
            conn.execute_query(sql)
            while True:
                header = conn.get_header()
                if header:
                    columns = [c[0] for c in header]
                    rows = [tuple(row[i] for i in range(len(columns))) for row in conn]
                    sets.append((columns, rows))
                if not conn.nextresult():
                    break
        except _mssql.MSSQLDatabaseException as exc:
            # Raised after the batch has run, when a statement in it failed. The
            # server's own message is normally in `messages` already.
            if not any(severity > 10 for severity, _text in messages):
                messages.append((16, str(exc)))
        return sets, messages
    finally:
        _mssql.min_error_severity = previous
        if conn is not None:
            conn.close()
