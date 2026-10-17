"""Compare how pyodbc (Microsoft ODBC Driver 18) and pymssql report a batch in which a statement fails.

Each case runs one batch on a fresh connection, collects everything the driver hands back
(result sets, row counts, messages, errors), then probes session state on the same connection.
Runs inside the sandbox tools container:

    docker compose -f sandbox/docker-compose.yml --profile tools run --rm tools python corpus/tools/odbc_errors.py
"""
import json
import os
import sys

import pymssql
import pyodbc

HOST = os.environ.get("MSSQL_HOST", "sqlserver")
PASSWORD = os.environ["MSSQL_SA_PASSWORD"]
DB = "odbc_probe"

SETUP = [
    f"IF DB_ID('{DB}') IS NOT NULL BEGIN ALTER DATABASE {DB} SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE {DB}; END",
    f"CREATE DATABASE {DB}",
]
SETUP_DB = [
    "CREATE TABLE dbo.t (id int PRIMARY KEY, v int NOT NULL)",
    """CREATE PROCEDURE dbo.p_fail AS
BEGIN
    INSERT dbo.t VALUES (40, 40);
    SELECT 'in proc' AS x;
    SELECT 1/0 AS boom;
    SELECT 'after error in proc' AS y;
    RETURN 5;
END""",
]

CASES = {
    "continue_after_error": "SET NOCOUNT OFF; SELECT 1 AS a; SELECT 1/0 AS b; SELECT 3 AS c; INSERT dbo.t VALUES (1, 1); SELECT COUNT(*) AS n FROM dbo.t;",
    "xact_abort_on": "SET XACT_ABORT ON; BEGIN TRAN; INSERT dbo.t VALUES (2, 2); SELECT 1 AS a; SELECT 1/0 AS b; SELECT 3 AS c;",
    "open_tran_left_open": "BEGIN TRAN; INSERT dbo.t VALUES (3, 3); SELECT 1/0 AS b; SELECT @@TRANCOUNT AS tc;",
    "try_catch": "BEGIN TRY SELECT 1 AS a; SELECT 1/0 AS b; END TRY BEGIN CATCH SELECT ERROR_NUMBER() AS n, ERROR_MESSAGE() AS m; END CATCH; SELECT 9 AS after_catch;",
    "error_in_procedure": "DECLARE @r int; EXEC @r = dbo.p_fail; SELECT @r AS ret;",
    "messages": "PRINT 'hello'; RAISERROR('info at 10', 10, 1); SELECT 1 AS a; RAISERROR('boom at 16', 16, 1); SELECT 2 AS b; PRINT 'bye';",
    "row_counts": "SET NOCOUNT OFF; UPDATE dbo.t SET v = v WHERE 1 = 0; INSERT dbo.t VALUES (5, 5); SELECT 1/0 AS b; DELETE dbo.t WHERE id = 5;",
    "session_state": "CREATE TABLE #s (x int); INSERT #s VALUES (1); SET DATEFIRST 3; SELECT 1/0 AS b; SELECT 'after' AS z;",
    "key_violation": "INSERT dbo.t VALUES (7, 7); INSERT dbo.t VALUES (7, 7); SELECT 'after duplicate' AS z;",
    "conversion_error": "SELECT 1 AS a; SELECT CAST('x' AS int) AS b; SELECT 'after cast' AS z;",
}

PROBE = ("SELECT @@TRANCOUNT AS trancount, XACT_STATE() AS xact_state, @@DATEFIRST AS datefirst, "
         "CASE WHEN OBJECT_ID('tempdb..#s') IS NULL THEN 0 ELSE 1 END AS temp_table_exists, "
         "(SELECT COUNT(*) FROM dbo.t) AS rows_in_t, (@@OPTIONS & 16384) / 16384 AS xact_abort")
RESET = "IF @@TRANCOUNT > 0 ROLLBACK; DELETE dbo.t;"


def odbc_connect(database):
    return pyodbc.connect(
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={HOST},1433;DATABASE={database};"
        f"UID=sa;PWD={PASSWORD};Encrypt=yes;TrustServerCertificate=yes",
        autocommit=True, timeout=10)


def mssql_connect(database):
    return pymssql.connect(server=HOST, user="sa", password=PASSWORD, database=database,
                           autocommit=True, tds_version="7.4", login_timeout=10)


def admin(statements, database="master"):
    conn = odbc_connect(database)
    for s in statements:
        conn.cursor().execute(s)
    conn.close()


def run_pyodbc(batch):
    conn = odbc_connect(DB)
    cur = conn.cursor()
    events = []
    try:
        cur.execute(batch)
        more = True
    except pyodbc.Error as exc:
        events.append({"error": f"{exc.args[0]} {exc.args[1][:120]}", "at": "execute"})
        more = True  # try to keep reading after the error
    guard = 0
    while more and guard < 30:
        guard += 1
        for m in (cur.messages or []):
            events.append({"message": m[1][:120]})
        if cur.description:
            events.append({"rows": [list(r) for r in cur.fetchall()], "columns": [d[0] for d in cur.description]})
        else:
            events.append({"rowcount": cur.rowcount})
        try:
            more = cur.nextset()
        except pyodbc.Error as exc:
            events.append({"error": f"{exc.args[0]} {exc.args[1][:120]}", "at": "nextset"})
            more = True
    probe = probe_state(conn.cursor(), pyodbc.Error)
    conn.cursor().execute(RESET)
    conn.close()
    return events, probe


def run_pymssql(batch):
    conn = mssql_connect(DB)
    messages = []
    conn._conn.set_msghandler(lambda *a: messages.append(str(a[-1])[:120] if a else ""))
    cur = conn.cursor()
    events = []
    try:
        cur.execute(batch)
        while True:
            for m in messages:
                events.append({"message": m})
            messages.clear()
            if cur.description:
                events.append({"rows": [list(r) for r in cur.fetchall()], "columns": [d[0] for d in cur.description]})
            else:
                events.append({"rowcount": cur.rowcount})
            if not cur.nextset():
                break
    except pymssql.Error as exc:
        for m in messages:
            events.append({"message": m})
        events.append({"error": str(exc.args[0]) + " " + str(exc.args[1] if len(exc.args) > 1 else "")[:120], "at": "execute/fetch"})
    probe = probe_state(conn.cursor(), pymssql.Error)
    try:
        conn.cursor().execute(RESET)
    except pymssql.Error:
        pass
    conn.close()
    return events, probe


def probe_state(cur, error_type):
    try:
        cur.execute(PROBE)
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, cur.fetchone()))
    except error_type as exc:
        return {"probe_error": str(exc)[:160]}


def main():
    admin(SETUP)
    admin(SETUP_DB, DB)
    results = {}
    for name, batch in CASES.items():
        results[name] = {"batch": batch}
        for driver, runner in (("pyodbc", run_pyodbc), ("pymssql", run_pymssql)):
            events, probe = runner(batch)
            results[name][driver] = {"events": events, "after": probe}
    admin([SETUP[0]])
    json.dump(results, sys.stdout, indent=1, default=str)


if __name__ == "__main__":
    main()
