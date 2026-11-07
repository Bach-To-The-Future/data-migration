"""Grant the sandbox's extractor login, as a client's DBA would, by Annex F's grant set.

    python sandbox/grant_extractor.py [database ...]

Runs inside the sandbox tools container (corpus/build.py calls it) and connects as sa,
playing the DBA. With no database named it grants on the three corpus databases.
Idempotent: the login's password is reset to MIGRT_EXTRACTOR_PASSWORD and missing users
and grants are added. The engine itself never runs this; it only checks the result.
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "corpus" / "tools"))
import db  # noqa: E402  (the corpus tools' sa connection)

LOGIN = "migration_extractor"
DEFAULT_DATABASES = ["RetailSample", "AdventureWorks2022", "WideWorldImporters"]
MSDB_TABLES = ["sysjobs", "sysjobsteps", "sysschedules", "sysjobschedules", "syscategories"]


def quote_literal(text):
    return "N'" + text.replace("'", "''") + "'"


def ensure_user(conn, database):
    db.execute(conn, f"""
USE [{database}];
IF DATABASE_PRINCIPAL_ID(N'{LOGIN}') IS NULL CREATE USER [{LOGIN}] FOR LOGIN [{LOGIN}];""")


def main():
    databases = sys.argv[1:] or DEFAULT_DATABASES
    password = quote_literal(os.environ["MIGRT_EXTRACTOR_PASSWORD"])
    db.wait_for_server()
    conn = db.connect()
    db.execute(conn, f"""
IF SUSER_ID(N'{LOGIN}') IS NULL CREATE LOGIN [{LOGIN}] WITH PASSWORD = {password};
ELSE ALTER LOGIN [{LOGIN}] WITH PASSWORD = {password};""")
    for database in databases:
        if not db.query(conn, "SELECT 1 FROM sys.databases WHERE name = %s", (database,)):
            print(f"  {database}: not on the server, skipped")
            continue
        ensure_user(conn, database)
        db.execute(conn, f"""
USE [{database}];
GRANT VIEW DEFINITION TO [{LOGIN}];
GRANT SELECT ON sys.sql_expression_dependencies TO [{LOGIN}];""")
        print(f"  {database}: granted")
    ensure_user(conn, "msdb")
    db.execute(conn, "USE msdb;\n" + "\n".join(f"GRANT SELECT ON dbo.{t} TO [{LOGIN}];" for t in MSDB_TABLES))
    print("  msdb: granted")
    conn.close()


if __name__ == "__main__":
    main()
