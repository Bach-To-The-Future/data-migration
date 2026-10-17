using System.Text.Json;
using Microsoft.Data.SqlClient;

var pw = Environment.GetEnvironmentVariable("MSSQL_SA_PASSWORD");
string Cs(string db) => $"Server=sqlserver,1433;Database={db};User Id=sa;Password={pw};Encrypt=True;TrustServerCertificate=True";
void Admin(string db, params string[] sqls) { using var c = new SqlConnection(Cs(db)); c.Open(); foreach (var s in sqls) new SqlCommand(s, c).ExecuteNonQuery(); }

Admin("master", "IF DB_ID('sqlclient_probe') IS NOT NULL BEGIN ALTER DATABASE sqlclient_probe SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE sqlclient_probe; END", "CREATE DATABASE sqlclient_probe");
Admin("sqlclient_probe", "CREATE TABLE dbo.t (id int PRIMARY KEY, v int NOT NULL)",
  "CREATE PROCEDURE dbo.p_fail AS BEGIN INSERT dbo.t VALUES (40, 40); SELECT 'in proc' AS x; SELECT 1/0 AS boom; SELECT 'after error in proc' AS y; RETURN 5; END");

var cases = new Dictionary<string, string> {
  ["continue_after_error"] = "SET NOCOUNT OFF; SELECT 1 AS a; SELECT 1/0 AS b; SELECT 3 AS c; INSERT dbo.t VALUES (1, 1); SELECT COUNT(*) AS n FROM dbo.t;",
  ["xact_abort_on"] = "SET XACT_ABORT ON; BEGIN TRAN; INSERT dbo.t VALUES (2, 2); SELECT 1 AS a; SELECT 1/0 AS b; SELECT 3 AS c;",
  ["open_tran_left_open"] = "BEGIN TRAN; INSERT dbo.t VALUES (3, 3); SELECT 1/0 AS b; SELECT @@TRANCOUNT AS tc;",
  ["error_in_procedure"] = "DECLARE @r int; EXEC @r = dbo.p_fail; SELECT @r AS ret;",
  ["messages"] = "PRINT 'hello'; RAISERROR('info at 10', 10, 1); SELECT 1 AS a; RAISERROR('boom at 16', 16, 1); SELECT 2 AS b; PRINT 'bye';",
  ["row_counts"] = "SET NOCOUNT OFF; UPDATE dbo.t SET v = v WHERE 1 = 0; INSERT dbo.t VALUES (5, 5); SELECT 1/0 AS b; DELETE dbo.t WHERE id = 5;",
  ["key_violation"] = "INSERT dbo.t VALUES (7, 7); INSERT dbo.t VALUES (7, 7); SELECT 'after duplicate' AS z;",
  ["conversion_error"] = "SELECT 1 AS a; SELECT CAST('x' AS int) AS b; SELECT 'after cast' AS z;",
};
var probe = "SELECT @@TRANCOUNT, @@DATEFIRST, (SELECT COUNT(*) FROM dbo.t)";
var output = new Dictionary<string, object>();
foreach (var (name, batch) in cases) {
  var events = new List<string>();
  using var c = new SqlConnection(Cs("sqlclient_probe"));
  c.FireInfoMessageEventOnUserErrors = true;
  c.InfoMessage += (s, e) => { foreach (SqlError er in e.Errors) events.Add($"M[{er.Number},{er.Class}] {er.Message}"); };
  c.Open();
  var cmd = new SqlCommand(batch, c);
  cmd.StatementCompleted += (s, e) => events.Add($"done n={e.RecordCount}");
  try {
    using var r = cmd.ExecuteReader();
    do {
      if (r.FieldCount > 0) { var rows = new List<string>(); while (r.Read()) { var v = new object[r.FieldCount]; r.GetValues(v); rows.Add(string.Join(",", v)); } events.Add($"R[{string.Join(";", rows)}]"); }
    } while (r.NextResult());
  } catch (SqlException ex) { events.Add($"EXCEPTION {ex.Number}: {ex.Message}"); }
  string after;
  using (var p = new SqlCommand(probe, c).ExecuteReader()) { p.Read(); after = $"trancount={p[0]} datefirst={p[1]} rows_in_t={p[2]}"; }
  new SqlCommand("IF @@TRANCOUNT > 0 ROLLBACK; DELETE dbo.t;", c).ExecuteNonQuery();
  output[name] = new { events, after };
}
Admin("master", "ALTER DATABASE sqlclient_probe SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE sqlclient_probe");
Console.WriteLine(JsonSerializer.Serialize(output, new JsonSerializerOptions { WriteIndented = true }));
