import java.sql.*;
import java.util.*;

public class Probe {
    static String url(String db) {
        return "jdbc:sqlserver://sqlserver:1433;databaseName=" + db + ";user=sa;password=" + System.getenv("MSSQL_SA_PASSWORD")
            + ";encrypt=true;trustServerCertificate=true";
    }
    static void admin(String db, String... sqls) throws Exception {
        try (Connection c = DriverManager.getConnection(url(db)); Statement s = c.createStatement()) { for (String q : sqls) s.execute(q); }
    }
    static void warnings(SQLWarning w, List<String> ev) {
        for (; w != null; w = w.getNextWarning()) ev.add("W[" + w.getErrorCode() + "] " + w.getMessage());
    }
    public static void main(String[] args) throws Exception {
        admin("master", "IF DB_ID('jdbc_probe') IS NOT NULL BEGIN ALTER DATABASE jdbc_probe SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE jdbc_probe; END", "CREATE DATABASE jdbc_probe");
        admin("jdbc_probe", "CREATE TABLE dbo.t (id int PRIMARY KEY, v int NOT NULL)",
            "CREATE PROCEDURE dbo.p_fail AS BEGIN INSERT dbo.t VALUES (40, 40); SELECT 'in proc' AS x; SELECT 1/0 AS boom; SELECT 'after error in proc' AS y; RETURN 5; END");
        LinkedHashMap<String, String> cases = new LinkedHashMap<>();
        cases.put("continue_after_error", "SET NOCOUNT OFF; SELECT 1 AS a; SELECT 1/0 AS b; SELECT 3 AS c; INSERT dbo.t VALUES (1, 1); SELECT COUNT(*) AS n FROM dbo.t;");
        cases.put("xact_abort_on", "SET XACT_ABORT ON; BEGIN TRAN; INSERT dbo.t VALUES (2, 2); SELECT 1 AS a; SELECT 1/0 AS b; SELECT 3 AS c;");
        cases.put("open_tran_left_open", "BEGIN TRAN; INSERT dbo.t VALUES (3, 3); SELECT 1/0 AS b; SELECT @@TRANCOUNT AS tc;");
        cases.put("error_in_procedure", "DECLARE @r int; EXEC @r = dbo.p_fail; SELECT @r AS ret;");
        cases.put("messages", "PRINT 'hello'; RAISERROR('info at 10', 10, 1); SELECT 1 AS a; RAISERROR('boom at 16', 16, 1); SELECT 2 AS b; PRINT 'bye';");
        cases.put("row_counts", "SET NOCOUNT OFF; UPDATE dbo.t SET v = v WHERE 1 = 0; INSERT dbo.t VALUES (5, 5); SELECT 1/0 AS b; DELETE dbo.t WHERE id = 5;");
        cases.put("key_violation", "INSERT dbo.t VALUES (7, 7); INSERT dbo.t VALUES (7, 7); SELECT 'after duplicate' AS z;");
        cases.put("conversion_error", "SELECT 1 AS a; SELECT CAST('x' AS int) AS b; SELECT 'after cast' AS z;");
        for (Map.Entry<String, String> e : cases.entrySet()) {
            List<String> ev = new ArrayList<>();
            try (Connection c = DriverManager.getConnection(url("jdbc_probe")); Statement s = c.createStatement()) {
                boolean isRs;
                try { isRs = s.execute(e.getValue()); } catch (SQLException ex) { ev.add("E[" + ex.getErrorCode() + "] " + ex.getMessage()); isRs = false; }
                for (int guard = 0; guard < 40; guard++) {
                    warnings(s.getWarnings(), ev); s.clearWarnings();
                    if (isRs) {
                        List<String> rows = new ArrayList<>();
                        try (ResultSet rs = s.getResultSet()) { while (rs.next()) rows.add(String.valueOf(rs.getObject(1))); ev.add("R" + rows); }
                        catch (SQLException ex) { ev.add("R" + rows + " then E[" + ex.getErrorCode() + "] " + ex.getMessage()); }
                    } else {
                        int n = s.getUpdateCount();
                        if (n == -1) break;
                        ev.add("n=" + n);
                    }
                    try { isRs = s.getMoreResults(); }
                    catch (SQLException ex) { ev.add("E[" + ex.getErrorCode() + "] " + ex.getMessage()); isRs = false; if (s.getUpdateCount() == -1) { try { isRs = s.getMoreResults(); } catch (SQLException ex2) { ev.add("E2[" + ex2.getErrorCode() + "] " + ex2.getMessage()); } } }
                }
                String after;
                try (ResultSet p = c.createStatement().executeQuery("SELECT @@TRANCOUNT, @@DATEFIRST, (SELECT COUNT(*) FROM dbo.t)")) { p.next(); after = "trancount=" + p.getInt(1) + " datefirst=" + p.getInt(2) + " rows_in_t=" + p.getInt(3); }
                c.createStatement().execute("IF @@TRANCOUNT > 0 ROLLBACK; DELETE dbo.t;");
                System.out.println("== " + e.getKey() + "\n   " + String.join(" | ", ev) + "\n   after: " + after);
            }
        }
        admin("master", "ALTER DATABASE jdbc_probe SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE jdbc_probe");
    }
}
