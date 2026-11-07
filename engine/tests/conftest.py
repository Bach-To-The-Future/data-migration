"""Fixtures for the engine's tests. They run in the sandbox tools container, against the
sandbox SQL Server (with the corpus built and the extractor login granted) and Postgres."""
import os
import uuid

import pytest

from migrt import engine


@pytest.fixture
def engagement():
    """A fresh engagement with its own control database, dropped afterwards."""
    eng = engine.Engagement(name=f"test_{uuid.uuid4().hex[:10]}", control_server_dsn=os.environ["MIGRT_CONTROL_DSN"])
    yield eng
    engine.delete_engagement_data(eng)


def sandbox_server(login="migration_extractor", password=None, databases=("RetailSample",)):
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
