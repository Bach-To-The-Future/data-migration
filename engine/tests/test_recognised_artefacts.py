"""Recognised artefacts (issue #40, spec section 3, "Recognised artefacts"): the SSIS package
inventory, the secrets marked in packages, and the parts of SQL Agent job inventories that
need no parser. No server or .NET is needed, so these also run on the host:
`cd engine && PYTHONPATH=src python -m pytest tests/test_recognised_artefacts.py`."""
import pathlib

from migrt.core import secrets as core_secrets
from migrt.sources.sqlserver import jobs, secrets, ssis

REPO = pathlib.Path(__file__).parents[2]
SAMPLE_PACKAGE = REPO / "corpus" / "sample" / "artefacts" / "recognised" / "ssis" / "LoadDailySales.dtsx"

PACKAGE_WITH_A_PASSWORD = """<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:refId="Package" DTS:ObjectName="Nightly"
  DTS:ProtectionLevel="2" DTS:ExecutableType="Microsoft.Package">
  <DTS:ConnectionManagers>
    <DTS:ConnectionManager DTS:refId="Package.ConnectionManagers[Hq]" DTS:CreationName="OLEDB"
      DTS:DTSID="{11111111-1111-1111-1111-111111111111}" DTS:ObjectName="Hq">
      <DTS:ObjectData>
        <DTS:ConnectionManager DTS:ConnectionString="Data Source=hq;Initial Catalog=Sales;User ID=loader;Password=Hunter2!;">
          <DTS:Password DTS:Name="Password" Sensitive="1">Hunter2!</DTS:Password>
        </DTS:ConnectionManager>
      </DTS:ObjectData>
    </DTS:ConnectionManager>
  </DTS:ConnectionManagers>
  <DTS:Executables>
    <DTS:Executable DTS:refId="Package\\Load" DTS:ObjectName="Load" DTS:ExecutableType="Microsoft.ExecuteSQLTask">
      <DTS:ObjectData>
        <SQLTask:SqlTaskData xmlns:SQLTask="www.microsoft.com/sqlserver/dts/tasks/sqltask"
          SQLTask:Connection="{11111111-1111-1111-1111-111111111111}"
          SQLTask:SqlStatementSource="EXEC dbo.uspLoad @when = ?;" />
      </DTS:ObjectData>
    </DTS:Executable>
  </DTS:Executables>
</DTS:Executable>
"""

ENCRYPTED_PACKAGE = """<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:ObjectName="Locked" DTS:ProtectionLevel="3">
  <EncryptedData Type="http://www.w3.org/2001/04/xmlenc#Element" />
</DTS:Executable>
"""


def test_an_ssis_package_is_inventoried():
    found = ssis.inventory(SAMPLE_PACKAGE.read_text(encoding="utf-8"))
    facts = found.facts

    assert facts["package_name"] == "LoadDailySales" and facts["inventoried"] is True
    assert facts["protection_level"] == {"value": 1, "name": "EncryptSensitiveWithUserKey", "stated": True}
    assert [(c["name"], c["type"]) for c in facts["connections"]] == [("RetailSample", "OLEDB"),
                                                                      ("PriceFile", "FLATFILE")]
    assert [t["name"] for t in facts["tasks"]] == ["Truncate staging", "Load price file", "Apply prices"]
    assert facts["tasks"][0]["connection"] == "RetailSample"
    assert [(c["name"], c["class"]) for c in facts["components"]] == [
        ("Price file source", "Microsoft.FlatFileSource"), ("Trim and upper-case Sku", "Microsoft.DerivedColumn"),
        ("Count rows", "Microsoft.RowCount"), ("Staging destination", "Microsoft.OLEDBDestination")]
    assert [(p["from"], p["to"]) for p in facts["precedence_constraints"]] == [
        ("Package\\Truncate staging", "Package\\Load price file"), ("Package\\Load price file", "Package\\Apply prices")]
    assert {v["name"] for v in facts["variables"]} == {"InboxFolder", "LoadDate", "RowsStaged", "RemoveMissing"}
    # Where it names catalog objects: the SQL it sends, with the database its connection names.
    assert [(s["task"], s["text"], s["database"]) for s in found.sql] == [
        ("Package\\Truncate staging", "TRUNCATE TABLE stg.ProductPriceImport;", "RetailSample"),
        ("Package\\Apply prices", "EXEC sales.uspApplyPriceImport @RemoveMissing = ?;", "RetailSample")]
    assert [(t["component"], t["name"]) for t in found.tables] == [("Staging destination",
                                                                   "[stg].[ProductPriceImport]")]


def test_every_connection_string_and_sensitive_value_in_a_package_is_marked():
    found = ssis.inventory(PACKAGE_WITH_A_PASSWORD)

    [connection] = found.facts["connections"]
    marked = [m for m in found.marked if m["fact"] == "connections"]
    assert [(m["row"], m["column"], m["start"], m["length"]) for m in marked] == [
        (0, "connection_string", 0, len(connection["connection_string"]))]
    assert found.facts["sensitive_properties"] == [{"connection": "Hq", "property": "Password", "value": "Hunter2!"}]
    # The file itself, as the raw capture keeps it, is marked too.
    text = PACKAGE_WITH_A_PASSWORD
    values = {text[s.start:s.end] for s in secrets.in_package(text)}
    assert any("Password=Hunter2!" in v for v in values) and "Hunter2!" in values


def test_a_package_saved_with_encrypt_all_is_recorded_but_not_inventoried():
    found = ssis.inventory(ENCRYPTED_PACKAGE)

    assert found.facts["inventoried"] is False
    assert "encrypt all" in found.facts["not_inventoried_reason"]
    assert found.sql == [] and found.tables == []


def test_a_masked_cell_numbers_each_secret_by_its_place_in_the_object():
    marked = [{"fact": "steps", "row": 0, "column": "command", "start": 3, "length": 2, "pattern": "p"},
              {"fact": "steps", "row": 1, "column": "command", "start": 0, "length": 4, "pattern": "p"},
              {"fact": "steps", "row": 1, "column": "command", "start": 9, "length": 2, "pattern": "p"}]

    first, used_first = core_secrets.mask_cell("-U sa", marked, prefix="agent_job:J", fact="steps", row=0,
                                               column="command")
    second, used_second = core_secrets.mask_cell("pass and xx!", marked, prefix="agent_job:J", fact="steps", row=1,
                                                 column="command")

    assert first == "-U [secret: setting agent_job:J.secret_1]" and used_first == ["agent_job:J.secret_1"]
    assert second == ("[secret: setting agent_job:J.secret_2] and [secret: setting agent_job:J.secret_3]!")
    assert used_second == ["agent_job:J.secret_2", "agent_job:J.secret_3"]


def test_msdb_normalises_a_schedule_as_sp_verify_schedule_does():
    daily = {"name": "d", "enabled": 1, "freq_type": 4, "freq_interval": 1, "freq_subday_type": 0,
             "freq_subday_interval": 0, "freq_relative_interval": 0, "freq_recurrence_factor": 5,
             "active_start_date": None, "active_end_date": 99991231, "active_start_time": 0, "active_end_time": 235959}

    changed = jobs._verified(daily)

    assert (daily["freq_subday_type"], daily["freq_recurrence_factor"]) == (1, 0)
    assert {c["property"] for c in changed} == {"freq_subday_type", "freq_recurrence_factor"}
    once = {**daily, "freq_type": 1, "freq_interval": 3, "freq_subday_type": 4}
    jobs._verified(once)
    assert (once["freq_interval"], once["freq_subday_type"]) == (0, 0)


def test_an_ssis_step_names_the_package_it_runs():
    command = '/ISSERVER "\\"\\SSISDB\\Retail\\Loads\\LoadDailySales.dtsx\\"" /SERVER "\\"hq\\"" /Par x'

    found = jobs.package_path(command)

    assert found is not None and found[0] == "\\SSISDB\\Retail\\Loads\\LoadDailySales.dtsx"
    assert jobs.package_stem(found[0]) == jobs.package_stem("recognised/ssis/LoadDailySales.dtsx") == "loaddailysales"
    assert jobs.package_path("/FILE C:\\pkgs\\Nightly.dtsx /REPORTING E") == ("C:\\pkgs\\Nightly.dtsx", 6)
