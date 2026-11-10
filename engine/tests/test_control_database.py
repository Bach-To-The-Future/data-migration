"""The control database keeps snapshots immutably (spec section 14, ADR 0007). These tests
reach into Postgres because the claim is about storage: no one can change a stored fact."""
import psycopg
import pytest

from migrt import engine
from migrt.core import control_db


def _dsn(engagement):
    return control_db.ensure_database(engagement.control_server_dsn, engagement.name)


@pytest.mark.parametrize("statement", [
    "UPDATE catalog_fact SET value = '\"changed\"'",
    "DELETE FROM catalog_object",
    "UPDATE snapshot SET source = 'other'",
    "TRUNCATE catalog_fact CASCADE",
])
def test_a_stored_snapshot_cannot_be_changed(engagement, extractor_server, statement):
    engine.extract(engagement, extractor_server)

    with psycopg.connect(_dsn(engagement)) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="snapshots are immutable"):
            conn.execute(statement)


def test_rows_added_to_a_stored_snapshot_are_detected_on_reading_it(engagement, extractor_server):
    snapshot = engine.extract(engagement, extractor_server)
    with psycopg.connect(_dsn(engagement)) as conn:
        conn.execute("INSERT INTO catalog_object (snapshot_id, identity, kind, content_hash)"
                     " VALUES (%s, 'RetailSample.dbo.Planted', 'table', 'x')", (snapshot.id,))

    with pytest.raises(control_db.SnapshotCorrupted):
        engine.read_snapshot(engagement, snapshot.id)


def test_deleting_an_engagement_drops_its_control_database(extractor_server):
    import os
    from conftest import RUN
    eng = engine.Engagement(f"test_delete_me_{RUN}", os.environ["MIGRT_CONTROL_DSN"])
    engine.extract(eng, extractor_server)

    engine.delete_engagement_data(eng)

    with psycopg.connect(eng.control_server_dsn) as admin:
        assert admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (f"migrt_test_delete_me_{RUN}",)).fetchone() is None
