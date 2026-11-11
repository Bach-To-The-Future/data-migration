"""Session state (spec sections 6 and 7): what converted code carries in place of the legacy database session. The
starting values and the scoping of temp tables and identity values are compared with SQL Server."""
import json

import pytest

import migrt_spark as lib
from support_harness import C


def settings_file(tmp_path, *, secret_value=None):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "format": "migrt-settings/1", "engagement": "e", "snapshot": "s",
        "semantic": {"time_zone": {"value": "UTC", "origin": "read from the server"},
                     "transaction_mechanism": {"value": "compensating restore", "origin": "default"}},
        "fingerprint": "f",
        "runtime_identity": {"SUSER_SNAME": {"default": "databricks-user", "override": None},
                             "HOST_NAME": {"default": "cluster-1", "override": "my-host"}},
        "secrets": {"RetailSample.sales.vSecret.secret_1": {"value": secret_value, "required_by": [], "read_by_code": True}},
    }), encoding="utf-8")
    return path


def test_a_new_session_starts_where_a_new_sql_server_session_does(duo):
    expected = duo.tsql("SELECT @@ROWCOUNT AS rc, @@ERROR AS err, SCOPE_IDENTITY() AS si, @@IDENTITY AS ident, "
                        "@@TRANCOUNT AS tc")[0]
    ss = duo.session()
    assert (ss.rowcount, ss.at_error, ss.scope_identity(), ss.identity, ss.trancount) == \
        (expected["rc"], expected["err"], expected["si"], expected["ident"], expected["tc"])
    assert ss.error is None and ss.messages == [] and ss.values == {}


def test_print_keeps_messages_in_order_and_values_are_session_values(duo):
    ss = duo.session()
    ss.print("first")
    ss.print("second")
    ss.values["@@LANGUAGE"] = "us_english"
    assert ss.messages == ["first", "second"] and ss.values == {"@@LANGUAGE": "us_english"}


def test_a_marked_secret_is_read_from_the_settings_file(duo, tmp_path):
    ss = duo.session(settings=settings_file(tmp_path, secret_value="Hunter2!"))
    assert ss.setting("RetailSample.sales.vSecret.secret_1") == "Hunter2!"


def test_a_secret_the_client_has_not_filled_in_fails_loudly(duo, tmp_path):
    ss = duo.session(settings=settings_file(tmp_path))
    with pytest.raises(lib.SettingNotSet, match="RetailSample.sales.vSecret.secret_1"):
        ss.setting("RetailSample.sales.vSecret.secret_1")


def test_an_unknown_setting_fails_loudly(duo, tmp_path):
    ss = duo.session(settings=settings_file(tmp_path))
    with pytest.raises(lib.UnknownSetting, match="nope"):
        ss.setting("nope")


def test_a_session_with_no_settings_file_cannot_read_a_setting(duo):
    ss = duo.session()
    with pytest.raises(lib.UnknownSetting):
        ss.setting("anything")


def test_the_semantic_settings_and_runtime_identity_values_are_read_at_startup(duo, tmp_path):
    ss = duo.session(settings=settings_file(tmp_path))
    assert ss.semantic["time_zone"]["value"] == "UTC"
    assert ss.runtime_identity("SUSER_SNAME") == "databricks-user"          # the default
    assert ss.runtime_identity("HOST_NAME") == "my-host"                    # overridden at deployment


def test_a_settings_file_of_another_format_is_refused(duo, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"format": "migrt-settings/9"}), encoding="utf-8")
    with pytest.raises(ValueError, match="migrt-settings/9"):
        duo.session(settings=path)


# ---------------------------------------------------------------------- temp tables and call depth

T_COLUMNS = [C("Id", "int", nullable=False), C("Name", "varchar(10)")]


def temp_spec(duo, name="#t"):
    return lib.TableSpec(target=name, database=duo.database, schema="dbo", name=name,
                         columns=tuple(c.library() for c in T_COLUMNS))


def test_a_temp_table_is_a_scratch_table_held_under_its_original_name(duo):
    ss = duo.session()
    ss.create_temp_table(temp_spec(duo))
    ss.insert_values("#t", [{"Id": 1, "Name": "a"}, {"Id": 2, "Name": "b"}])
    assert sorted(tuple(r) for r in ss.table("#t").select("Id", "Name").collect()) == [(1, "a"), (2, "b")]
    ss.drop_temp_table("#t")
    with pytest.raises(lib.TsqlError) as err:
        ss.table("#t")
    assert (err.value.number, err.value.severity) == (208, 16)
    assert err.value.message == "Invalid object name '#t'."


def test_a_temp_table_created_in_a_called_procedure_is_dropped_when_it_returns(duo):
    duo.tsql("CREATE PROCEDURE {s}.uspMake AS BEGIN CREATE TABLE #made (Id int); INSERT #made VALUES (1) END")
    duo.tsql("EXEC {s}.uspMake")
    sql_error = duo.tsql_error("SELECT COUNT(*) FROM #made")

    ss = duo.session()
    with ss.scope():
        ss.create_temp_table(temp_spec(duo, "#made"))
    with pytest.raises(lib.TsqlError) as err:
        ss.table("#made")
    assert (err.value.number, err.value.message) == (sql_error.number, sql_error.text.replace("#made", "#made"))


def test_a_callee_may_shadow_its_callers_temp_table_and_the_callers_returns_when_it_ends(duo):
    duo.tsql("CREATE TABLE #t (Id int); INSERT #t VALUES (1), (2), (3)")
    duo.tsql("CREATE PROCEDURE {s}.uspShadow AS BEGIN CREATE TABLE #t (Id int); INSERT #t VALUES (9); "
             "SELECT COUNT(*) AS n FROM #t END")
    inside = duo.tsql("EXEC {s}.uspShadow")[0]["n"]
    after = duo.tsql("SELECT COUNT(*) AS n FROM #t")[0]["n"]

    ss = duo.session()
    ss.create_temp_table(temp_spec(duo))
    ss.insert_values("#t", [{"Id": 1}, {"Id": 2}, {"Id": 3}])
    with ss.scope():
        ss.create_temp_table(temp_spec(duo))
        ss.insert_values("#t", [{"Id": 9}])
        assert ss.table("#t").count() == inside
    assert ss.table("#t").count() == after


def test_a_table_variable_is_a_scratch_table_too_but_outside_transactions(duo):
    ss = duo.session()
    ss.create_table_variable(temp_spec(duo, "@v"))
    ss.insert_values("@v", [{"Id": 1}])
    assert ss.table("@v").count() == 1
    assert ss.tables_in_scope() == ["@v"]


def test_the_scratch_tables_of_a_run_are_dropped_when_it_ends_and_are_registered_while_it_lives(duo):
    ss = duo.session(run="r1")
    ss.create_temp_table(temp_spec(duo))
    registered = duo.spark.table(duo.state_table).where("kind = 'scratch'").collect()
    assert [r.run for r in registered] == ["r1"]
    prefix = registered[0].key.split("|")[1]
    assert any(t.name.startswith(prefix) for t in duo.spark.catalog.listTables(duo.schema))
    ss.close()
    assert not any(t.name.startswith(prefix) for t in duo.spark.catalog.listTables(duo.schema))
    assert duo.spark.table(duo.state_table).where("kind = 'scratch'").count() == 0


# ---------------------------------------------------------------------- the error being handled

def test_the_error_being_handled_is_visible_inside_the_handler_only(duo):
    ss = duo.session()
    err = lib.TsqlError(2627, 14, 1, "Violation of PRIMARY KEY constraint 'PK'.")
    assert ss.error is None
    with ss.handling(err):
        assert ss.error is err
        with ss.handling(lib.TsqlError(547, 16, 0, "inner")):
            assert ss.error is not None and ss.error.number == 547
        assert ss.error is err
    assert ss.error is None


def test_a_spark_session_that_already_marks_its_commits_for_another_run_is_refused(duo):
    spark = duo.spark.newSession()
    first = lib.SessionState.open(spark, job="a", run="r1", state_table=duo.state_table,
                                  config=lib.Config(scratch_schema=duo.schema, database=duo.database))
    with pytest.raises(ValueError, match="own session"):
        lib.SessionState.open(spark, job="a", run="r2", state_table=duo.state_table,
                              config=lib.Config(scratch_schema=duo.schema, database=duo.database))
    first.close()


def test_the_default_collation_of_the_database_comes_from_the_settings_file(duo, tmp_path):
    duo.table("Names", [C("Id", "int"), C("Name", "varchar(10)")], uniques=(lib.Unique("UQ_Names", ("Name",)),))
    settings = json.loads(settings_file(tmp_path).read_text(encoding="utf-8"))
    settings["semantic"]["databases"] = {duo.database: {"default_collation": {"value": "Latin1_General_CS_AS", "origin": "read"}}}
    ss = duo.session(settings=settings)
    assert ss.default_collation == "Latin1_General_CS_AS"
    ss.insert_values(duo.target("Names"), [{"Id": 1, "Name": "a"}, {"Id": 2, "Name": "A"}])      # distinct when case-sensitive
    assert duo.delta_rows("Names", ["Id"], "Id") == [(1,), (2,)]


def test_a_failed_heartbeat_is_raised_when_the_run_ends_even_if_it_never_wrote_again(duo):
    ss = duo.session()
    assert ss._heartbeat is not None
    ss._heartbeat.error = RuntimeError("the state table could not be reached")
    with pytest.raises(lib.LeaseLost, match="heartbeat failed"):
        ss.close()
