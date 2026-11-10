"""Fixtures for the engine's tests. They run in the sandbox tools container, against the
sandbox SQL Server (with the corpus built and the extractor login granted) and Postgres."""
import os
import re
import uuid

import pytest

# Sessions working in parallel share one sandbox SQL Server and one Postgres server
# (docs/agents/worktrees.md). Every scratch database, schema and engagement the tests create
# carries this run's suffix, so no run drops or reads another's. MIGRT_TEST_RUN fixes it, for
# a test that starts runs of its own.
RUN = os.environ.get("MIGRT_TEST_RUN") or uuid.uuid4().hex[:8]
assert re.fullmatch(r"[0-9a-f]{1,16}", RUN), "MIGRT_TEST_RUN is hexadecimal"


@pytest.fixture
def engagement():
    """A fresh engagement with its own control database, dropped afterwards."""
    from migrt import engine

    eng = engine.Engagement(name=f"test_{RUN}_{uuid.uuid4().hex[:6]}", control_server_dsn=os.environ["MIGRT_CONTROL_DSN"])
    yield eng
    engine.delete_engagement_data(eng)


def sandbox_server(login="migration_extractor", password=None, databases=("RetailSample",)):
    from migrt import engine

    return engine.SqlServerSource(
        host=os.environ.get("MSSQL_HOST", "sqlserver"),
        port=1433,
        login=login,
        password=password if password is not None else os.environ["MIGRT_EXTRACTOR_PASSWORD"],
        databases=list(databases),
    )


@pytest.fixture
def extractor_server():
    return sandbox_server()


SCRATCH = f"MigrtExtractTest_{RUN}"
# A case-sensitive scratch database with what the corpus lacks: a synonym, a sequence, user
# types, an encrypted procedure, a disabled and an ordered trigger, a database trigger,
# likely secrets, a name Unity Catalog refuses and names differing only by case.
SCRATCH_BATCHES = [
    "CREATE SCHEMA s",
    "CREATE TYPE dbo.Code FROM nvarchar(10) NOT NULL",
    "CREATE TYPE s.Lines AS TABLE (Id int PRIMARY KEY, Qty int NOT NULL CHECK (Qty > 0))",
    "CREATE SEQUENCE s.Seq AS bigint START WITH 100 INCREMENT BY 5",
    """CREATE TABLE s.Parent (
        Id int IDENTITY(10, 2) CONSTRAINT PK_Parent PRIMARY KEY,
        Code dbo.Code NOT NULL CONSTRAINT UQ_Parent_Code UNIQUE,
        Note varchar(20) COLLATE Latin1_General_CI_AS NULL CONSTRAINT DF_Parent_Note DEFAULT ('none'),
        Twice AS (Id * 2) PERSISTED,
        CONSTRAINT CK_Parent_Note CHECK (Note <> 'bad'))""",
    """CREATE TABLE s.Child (
        Id int NOT NULL,
        ParentId int NOT NULL CONSTRAINT FK_Child_Parent REFERENCES s.Parent (Id) ON DELETE CASCADE,
        Email varchar(50) NULL)""",
    "CREATE UNIQUE INDEX UX_Child_Email ON s.Child (Email) INCLUDE (ParentId) WHERE Email IS NOT NULL",
    "CREATE TABLE s.[Order Lines] (Id int NOT NULL, Qty int NOT NULL)",
    "CREATE TABLE s.Totals (Id int NOT NULL)",
    "CREATE TABLE s.TOTALS (Id int NOT NULL)",
    "CREATE SYNONYM dbo.Parents FOR s.Parent",
    "CREATE SYNONYM dbo.Remote FOR OtherDb.dbo.Thing",
    "CREATE VIEW s.vParents AS SELECT p.Id, p.Twice FROM dbo.Parents AS p",
    "CREATE VIEW s.[v Order Lines] AS SELECT l.Id, l.Qty FROM [Order Lines] AS l",
    "CREATE VIEW s.vSecret AS\n-- Server=hq;User ID=loader;Password=Hunter2!;\nSELECT p.Id FROM s.Parent AS p",
    "CREATE FUNCTION s.fnDouble (@x int = 4) RETURNS int AS BEGIN RETURN @x * 2 END",
    "CREATE FUNCTION s.fnChildren (@parent int) RETURNS TABLE AS RETURN SELECT c.Id FROM s.Child AS c WHERE c.ParentId = @parent",
    """CREATE PROCEDURE s.uspAdd @code dbo.Code, @note varchar(20) = 'n/a', @id int OUTPUT AS
    BEGIN
        INSERT INTO s.Parent (Code, Note) VALUES (@code, @note);
        SET @id = SCOPE_IDENTITY();
        SELECT NEXT VALUE FOR s.Seq;
        EXEC s.uspLog;
    END""",
    "CREATE PROCEDURE s.uspCaller AS SELECT s.fnDouble(1) AS d, f.Id FROM s.fnChildren(1) AS f",
    "CREATE PROCEDURE s.uspLink AS EXEC sp_addlinkedsrvlogin 'hq', 'false', NULL, 'loader', 'Hunter2!'",
    "CREATE PROCEDURE s.uspHidden @p int = 1 WITH ENCRYPTION AS SELECT @p AS one",
    "CREATE TRIGGER s.trParentA ON s.Parent AFTER INSERT, UPDATE AS SET NOCOUNT ON",
    "CREATE TRIGGER s.trParentB ON s.Parent AFTER INSERT AS SET NOCOUNT ON",
    "EXEC sp_settriggerorder 's.trParentB', 'First', 'INSERT'",
    "CREATE TRIGGER s.trChild ON s.Child INSTEAD OF INSERT AS SELECT i.Id FROM inserted AS i",
    "DISABLE TRIGGER s.trChild ON s.Child",
    "CREATE TRIGGER trDdl ON DATABASE FOR CREATE_TABLE AS SET NOCOUNT ON",
    # Annex F's grant set, as the client's DBA would run it.
    "CREATE USER [migration_extractor] FOR LOGIN [migration_extractor]",
    "GRANT VIEW DEFINITION TO [migration_extractor]",
    "GRANT SELECT ON sys.sql_expression_dependencies TO [migration_extractor]",
]


def sa_connection(database="master"):
    from migrt.sources.sqlserver.connection import Connection

    return Connection(os.environ.get("MSSQL_HOST", "sqlserver"), 1433, "sa", os.environ["MSSQL_SA_PASSWORD"], database)


def _drop_scratch(conn):
    conn.query(f"IF DB_ID(N'{SCRATCH}') IS NOT NULL BEGIN ALTER DATABASE [{SCRATCH}] SET SINGLE_USER WITH ROLLBACK "
               f"IMMEDIATE; DROP DATABASE [{SCRATCH}]; END")


@pytest.fixture(scope="session")
def scratch_database():
    """The scratch database's name; created for the test session and dropped after it."""
    with sa_connection() as master:
        _drop_scratch(master)
        master.query(f"CREATE DATABASE [{SCRATCH}] COLLATE Latin1_General_CS_AS")
    with sa_connection(SCRATCH) as conn:
        for batch in SCRATCH_BATCHES:
            conn.query(batch)
    yield SCRATCH
    with sa_connection() as master:
        _drop_scratch(master)


CORPUS_DATABASES = ("RetailSample", "AdventureWorks2022", "WideWorldImporters")


@pytest.fixture(scope="session")
def corpus_snapshot():
    """The three corpus databases extracted live once for the session, with no control database."""
    from migrt.sources.sqlserver import extractor

    address = extractor.ServerAddress(os.environ.get("MSSQL_HOST", "sqlserver"), 1433, "migration_extractor",
                                      os.environ["MIGRT_EXTRACTOR_PASSWORD"])
    return extractor.extract(address, list(CORPUS_DATABASES))
