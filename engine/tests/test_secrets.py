"""Likely secrets (spec section 3, "Secrets"): SQL Server's fixed patterns mark spans in
source text, job-step commands and raw rows, and the core masks a marked span wherever
text leaves the client's own catalog, replacing it with a named setting."""
from migrt.core import secrets
from migrt.sources.sqlserver import secrets as patterns


def marked(text, **kw):
    return [text[s.start:s.start + s.length] for s in patterns.in_text(text, **kw)]


def test_a_connection_strings_login_and_password_are_marked():
    text = "SELECT * FROM OPENDATASOURCE('MSOLEDBSQL', 'Data Source=hq;User ID=loader;Password=Hunter2!;')"
    assert marked(text) == ["loader", "Hunter2!"]


def test_openrowset_credentials_are_marked():
    text = "SELECT * FROM OPENROWSET('MSOLEDBSQL', 'hq'; 'loader'; 'S3cr3t', 'SELECT 1')"
    assert marked(text) == ["loader", "S3cr3t"]


def test_linked_server_logins_are_marked_named_or_positional():
    named = "EXEC sp_addlinkedsrvlogin @rmtsrvname = 'hq', @useself = 'false', @rmtuser = 'loader', @rmtpassword = 'pw1'"
    positional = "EXEC sp_addlinkedsrvlogin 'hq', 'false', NULL, 'loader', 'pw2'"
    assert marked(named) == ["loader", "pw1"]
    assert marked(positional) == ["loader", "pw2"]


def test_password_and_secret_clauses_are_marked():
    text = ("CREATE CREDENTIAL c WITH IDENTITY = 'svc', SECRET = 'abc''def';\n"
            "OPEN SYMMETRIC KEY k DECRYPTION BY PASSWORD = N'xyz';")
    assert marked(text) == ["abc''def", "xyz"]


def test_command_line_credentials_are_marked_only_in_command_steps():
    command = "sqlcmd -S hq -U loader -P Hunter2! -i load.sql"
    assert marked(command, command_line=True) == ["loader", "Hunter2!"]
    assert marked("SELECT @total -Price FROM t") == []


def test_text_without_credentials_marks_nothing():
    assert marked("CREATE VIEW v AS SELECT OrderId, Status FROM sales.[Order] -- no secrets here") == []


def test_a_secret_is_masked_with_a_named_setting_and_nothing_of_it_is_left():
    text = "x = 'Data Source=hq;Password=Hunter2!;' -- Password=Other1"
    spans = patterns.in_text(text)

    masked, settings = secrets.mask(text, spans, prefix="RetailSample.sales.v")

    assert "Hunter2!" not in masked and "Other1" not in masked
    assert settings == ["RetailSample.sales.v.secret_1", "RetailSample.sales.v.secret_2"]
    assert masked == ("x = 'Data Source=hq;Password=[secret: setting RetailSample.sales.v.secret_1];' "
                      "-- Password=[secret: setting RetailSample.sales.v.secret_2]")


def test_masking_part_of_a_text_uses_the_offsets_of_the_whole():
    text = "-- Password=Hunter2!\nSELECT 1"
    spans = patterns.in_text(text)
    comment = text[:20]

    masked, settings = secrets.mask(comment, spans, prefix="p", offset=0)
    assert masked == "-- Password=[secret: setting p.secret_1]"
    tail, none = secrets.mask(text[21:], spans, prefix="p", offset=21)
    assert (tail, none) == ("SELECT 1", [])


def test_a_raw_rows_provider_string_is_always_marked():
    rows = [{"name": "HQ", "provider_string": "Server=hq;Trusted_Connection=yes"}, {"name": "LOCAL", "provider_string": None}]
    found = patterns.in_rows("sys.servers", rows)
    assert [(s.row, s.column, s.start, s.length) for s in found] == [(0, "provider_string", 0, 32)]


def test_raw_rows_are_scanned_with_the_text_patterns():
    rows = [{"object_id": 5, "definition": "CREATE PROC p AS SELECT * FROM OPENROWSET('x', 'hq'; 'u'; 'pw', 't')"}]
    found = patterns.in_rows("sys.sql_modules", rows)
    assert [rows[0]["definition"][s.start:s.start + s.length] for s in found] == ["u", "pw"]
