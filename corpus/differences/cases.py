"""Test cases for the 14 differences the research could only infer.

docs/research/tsql-spark-silent-differences.md marks 14 of its 82 differences
as "inferred": one side's behaviour is documented, the other's is deduced.
Each case here runs both sides and lets `judge` say whether the claimed
difference is real. Ids are the row ids in that file.

A case has:
  tsql   one batch, or a list of batches, run on SQL Server to the end even
         if a statement fails; `judge` sees the last batch as
         {"sets": [rows, ...], "errors": [text, ...]} (or a list of those
         when `all_batches` is set)
  spark  a function taking (spark, attempt) and returning a dict
  judge  a function taking (tsql_observation, spark_observation) and
         returning (verdict, note); verdict is "confirmed", "refuted" or
         "partly confirmed"
"""

CONFIRMED, REFUTED, PARTLY = "confirmed", "refuted", "partly confirmed"


def rows(spark, sql):
    return [tuple(r) for r in spark.sql(sql).collect()]


# ------------------------------------------------------------------ 2.2

KEYS_SQL = """
SELECT
  (SELECT COUNT(*) FROM (VALUES ('abc')) AS a(k) JOIN (VALUES ('ABC')) AS b(k) ON a.k = b.k) AS join_rows,
  (SELECT COUNT(*) FROM (SELECT k FROM (VALUES ('abc'), ('ABC'), ('Abc')) AS v(k) GROUP BY k) AS g) AS group_count,
  (SELECT COUNT(*) FROM (SELECT DISTINCT k FROM (VALUES ('abc'), ('ABC'), ('Abc')) AS v(k)) AS d) AS distinct_count,
  (SELECT COUNT(*) FROM (VALUES ('abc')) AS v(k) WHERE k IN ('ABC')) AS in_rows,
  (SELECT COUNT(*) FROM (SELECT 'abc' AS k UNION SELECT 'ABC') AS u) AS union_rows,
  (SELECT COUNT(*) FROM (SELECT 'abc' AS k INTERSECT SELECT 'ABC') AS i) AS intersect_rows
"""


def spark_2_2(spark, attempt):
    return {"counts": attempt(lambda: rows(spark, KEYS_SQL)[0])}


def judge_2_2(t, s):
    t_counts, s_counts = t["sets"][0][0], s["counts"].get("ok")
    if tuple(t_counts) == (1, 1, 1, 1, 1, 1) and tuple(s_counts or ()) == (0, 3, 3, 0, 2, 0):
        return CONFIRMED, "T-SQL treats the three spellings as one key in every operator; Spark treats them as three."
    return REFUTED, "The counts are not the ones the research predicted."


# ------------------------------------------------------------------ 3.11

def spark_3_11(spark, attempt):
    return {"quotients": attempt(lambda: tuple(str(v) for v in rows(spark, """
        SELECT CAST(10 AS DECIMAL(19, 4)) / CAST(3 AS DECIMAL(19, 4)),
               CAST(2 AS DECIMAL(19, 4)) / CAST(3 AS DECIMAL(19, 4)),
               CAST(1 AS DECIMAL(19, 4)) / CAST(3 AS DECIMAL(19, 4)) * 3""")[0]))}


def judge_3_11(t, s):
    t_q = str(t["sets"][0][0][0])
    s_q = (s["quotients"].get("ok") or [""])[0]
    t_places = len(t_q.partition(".")[2])
    s_places = len(s_q.partition(".")[2])
    if t_places == 4 and s_places > 4:
        note = f"money / money keeps 4 decimal places ({t_q}); DECIMAL(19, 4) division keeps {s_places} ({s_q})."
        if str(t["sets"][0][0][1]) == "0.6666":
            note += " T-SQL truncates the quotient (2 / 3 = 0.6666), it does not round."
        return CONFIRMED, note
    return REFUTED, f"T-SQL gave {t_q}, Spark gave {s_q}."


# ------------------------------------------------------------------ 3.12

def spark_3_12(spark, attempt):
    return {
        "isnumeric_function": attempt(lambda: rows(spark, "SELECT isnumeric('$')")),
        "try_cast_emulation": attempt(lambda: rows(spark, """
            SELECT try_cast('$' AS DOUBLE) IS NOT NULL, try_cast('+' AS DOUBLE) IS NOT NULL,
                   try_cast('-' AS DOUBLE) IS NOT NULL, try_cast('1e5' AS DOUBLE) IS NOT NULL,
                   try_cast(',' AS DOUBLE) IS NOT NULL""")[0]),
    }


def judge_3_12(t, s):
    t_row = tuple(t["sets"][0][0])
    emulation = tuple(s["try_cast_emulation"].get("ok") or ())
    no_builtin = "error" in s["isnumeric_function"]
    if t_row[:3] == (1, 1, 1) and no_builtin and emulation[:3] == (False, False, False):
        return CONFIRMED, "ISNUMERIC is 1 for '$', '+' and '-'; Spark has no ISNUMERIC and try_cast says none of them is a number."
    return REFUTED, f"T-SQL gave {t_row}; Spark built-in present: {not no_builtin}; emulation gave {emulation}."


# ------------------------------------------------------------------ 6.4

def spark_6_4(spark, attempt):
    return {
        "scope_identity": attempt(lambda: rows(spark, "SELECT scope_identity()")),
        "at_at_identity": attempt(lambda: rows(spark, "SELECT @@IDENTITY")),
        "ident_current": attempt(lambda: rows(spark, "SELECT ident_current('d64')")),
        "delta_identity_column": attempt(lambda: rows(
            spark, "CREATE OR REPLACE TABLE d64 (id BIGINT GENERATED ALWAYS AS IDENTITY, v INT) USING delta")),
    }


def judge_6_4(t, s):
    t_row = tuple(t["sets"][0][0])
    missing = all("error" in s[k] for k in ("scope_identity", "at_at_identity", "ident_current"))
    if t_row == (15, 15, 15) and missing:
        note = "All three return the last identity value in T-SQL; none of them exists in Spark."
        if "error" in s["delta_identity_column"]:
            note += " Open-source Delta 4.0 also rejects identity columns in SQL DDL."
        return CONFIRMED, note
    return REFUTED, f"T-SQL gave {t_row}; Spark missing all three: {missing}."


# ------------------------------------------------------------------ 7.2

def spark_7_2(spark, attempt):
    spark.sql("CREATE OR REPLACE TABLE d72 (v INT) USING delta")
    spark.sql("ALTER TABLE d72 ADD CONSTRAINT ck_d72 CHECK (v > 0)")

    def script():
        spark.sql("INSERT INTO d72 VALUES (1)")
        spark.sql("INSERT INTO d72 VALUES (-1)")
        spark.sql("INSERT INTO d72 VALUES (3)")

    outcome = attempt(script)
    return {"script": outcome, "rows_after": [r[0] for r in rows(spark, "SELECT v FROM d72 ORDER BY v")]}


def judge_7_2(t, s):
    run, after = t[-2], t[-1]
    t_rows = [r[0] for r in after["sets"][0]]
    if t_rows == [1, 3] and run["errors"] and s["rows_after"] == [1] and "error" in s["script"]:
        return CONFIRMED, "T-SQL reports the failed insert and still runs the third; the Python script stops at the second."
    return REFUTED, f"T-SQL rows {t_rows}, errors {run['errors']}; Spark rows {s['rows_after']}."


# ------------------------------------------------------------------ 7.6

def spark_7_6(spark, attempt):
    def guarded(sql):
        try:
            spark.sql(sql).collect()
            return "not caught"
        except Exception:
            return "caught"

    return {
        "python_except_unresolved_column": guarded("SELECT no_such_column FROM VALUES (1) AS t(a)"),
        "python_except_missing_table": guarded("SELECT * FROM no_such_table_76"),
        "sql_handler_missing_table": attempt(lambda: rows(spark, """
            BEGIN
              DECLARE EXIT HANDLER FOR SQLEXCEPTION
                BEGIN SELECT 'caught' AS outcome; END;
              SELECT * FROM no_such_table_76;
            END""")),
    }


def judge_7_6(t, s):
    steps = [r[0] for r in t[-1]["sets"][0]]
    missing_errors = t[-4]["errors"]
    tsql_as_claimed = (
        "warning: not caught" in steps
        and "divide by zero: caught" in steps
        and not any(step.startswith("missing table, same level") for step in steps)
        and bool(missing_errors)
    )
    spark_catches = (s["python_except_unresolved_column"] == "caught" and s["python_except_missing_table"] == "caught")
    if tsql_as_claimed and spark_catches:
        note = ("In T-SQL, CATCH does not run for a severity-10 message, and does not run for a missing table "
                "referenced at the same level: the procedure stops with an error instead. It does run for divide by zero")
        if "missing table, one level down: caught by the caller" in steps:
            note += ", and for the same missing table when it is inside a called procedure"
        note += ". A Python except catches the missing table and an unresolved column."
        handler = s["sql_handler_missing_table"]
        note += (" A Spark SQL exit handler could not be tried: SQL scripting is off by default in this Spark version."
                 if "SQL_SCRIPTING" in handler.get("error", "")
                 else " The Spark SQL script with an exit handler failed for another reason; see results.json."
                 if "error" in handler
                 else " A Spark SQL SQLEXCEPTION handler also caught the missing table.")
        return CONFIRMED, note
    return REFUTED, f"T-SQL steps logged: {steps}; errors for the missing table: {missing_errors}; Spark: {s}."


# ------------------------------------------------------------------ 8.1

MERGE_8_1 = """
MERGE INTO d81_t AS t USING d81_s AS s ON t.code = s.code
WHEN MATCHED THEN UPDATE SET v = s.v
WHEN NOT MATCHED THEN INSERT (code, v) VALUES (s.code, s.v)"""


def spark_8_1(spark, attempt):
    spark.sql("CREATE OR REPLACE TABLE d81_t (code STRING, v INT) USING delta")
    spark.sql("CREATE OR REPLACE TABLE d81_s (code STRING, v INT) USING delta")
    spark.sql("INSERT INTO d81_t VALUES ('AB01', 1)")
    spark.sql("INSERT INTO d81_s VALUES ('ab01  ', 2)")
    outcome = attempt(lambda: spark.sql(MERGE_8_1).collect() and None)
    return {"merge": outcome, "rows_after": rows(spark, "SELECT code, v FROM d81_t ORDER BY v")}


def judge_8_1(t, s):
    t_rows = [tuple(r) for r in t["sets"][0]]
    if t_rows == [("AB01", 2)] and len(s["rows_after"]) == 2:
        return CONFIRMED, "T-SQL matched 'ab01  ' to 'AB01' and updated it; Spark found no match and inserted a second row."
    return REFUTED, f"T-SQL rows {t_rows}, Spark rows {s['rows_after']}."


# ------------------------------------------------------------------ 8.3

def spark_8_3(spark, attempt):
    spark.sql("CREATE OR REPLACE TABLE d83_t (k INT, v INT) USING delta")
    spark.sql("INSERT INTO d83_t VALUES (1, 10)")
    return {"merge_with_output": attempt(lambda: rows(spark, """
        MERGE INTO d83_t AS t USING (SELECT * FROM VALUES (1, 11), (2, 20) AS s(k, v)) AS s ON t.k = s.k
        WHEN MATCHED THEN UPDATE SET v = s.v
        WHEN NOT MATCHED THEN INSERT (k, v) VALUES (s.k, s.v)
        OUTPUT $action, inserted.k, deleted.v, inserted.v"""))}


def judge_8_3(t, s):
    t_rows = sorted(tuple(r) for r in t["sets"][0])
    if t_rows == [("INSERT", 2, None, 20), ("UPDATE", 1, 10, 11)] and "error" in s["merge_with_output"]:
        return CONFIRMED, "T-SQL returns one row per action; Spark rejects the OUTPUT clause as a syntax error."
    return REFUTED, f"T-SQL rows {t_rows}; Spark: {s['merge_with_output']}."


# ------------------------------------------------------------------ 8.4

def spark_8_4(spark, attempt):
    spark.sql("CREATE OR REPLACE TABLE d84_t (k INT, v INT) USING delta")
    return {"create_trigger": attempt(lambda: rows(
        spark, "CREATE TRIGGER d84_ins AFTER INSERT ON d84_t FOR EACH ROW INSERT INTO d84_log VALUES ('insert', 1)"))}


def judge_8_4(t, s):
    t_rows = [tuple(r) for r in t["sets"][0]]
    if t_rows == [("insert", 1), ("update", 1)] and "error" in s["create_trigger"]:
        return CONFIRMED, "Both AFTER triggers fired once for the MERGE in T-SQL; Spark has no CREATE TRIGGER."
    return REFUTED, f"T-SQL trigger log {t_rows}; Spark: {s['create_trigger']}."


# ------------------------------------------------------------------ 8.5

def spark_8_5(spark, attempt):
    spark.sql("CREATE OR REPLACE TABLE d85_t (k INT, v INT) USING delta")
    spark.sql("INSERT INTO d85_t VALUES (1, 10), (2, 20)")
    outcome = attempt(lambda: spark.sql("""
        MERGE INTO d85_t AS t USING (SELECT * FROM VALUES (1), (1) AS s(k)) AS s ON t.k = s.k
        WHEN MATCHED THEN DELETE""").collect() and None)
    return {"merge": outcome, "rows_after": [r[0] for r in rows(spark, "SELECT k FROM d85_t ORDER BY k")]}


def judge_8_5(t, s):
    t_rows = [r[0] for r in t["sets"][0]]
    t_failed, s_failed = bool(t["errors"]), "error" in s["merge"]
    if t_failed and not s_failed:
        return CONFIRMED, "T-SQL refuses to delete a row matched by two source rows; Spark deletes it."
    if not t_failed and not s_failed and t_rows == s["rows_after"] == [2]:
        return REFUTED, "No difference: both delete the row matched by two source rows and leave the other."
    return PARTLY, f"T-SQL failed: {t_failed}, rows {t_rows}; Spark failed: {s_failed}, rows {s['rows_after']}."


# ------------------------------------------------------------------ 9.4

def spark_9_4(spark, attempt):
    return {"substrings": attempt(lambda: rows(
        spark, "SELECT substring('abcdef', 0, 3), substring('abcdef', -1, 3), substring('abcdef', 1, 3)")[0])}


def judge_9_4(t, s):
    t_row, s_row = tuple(t["sets"][0][0]), tuple(s["substrings"].get("ok") or ())
    if t_row[0] == "ab" and s_row and s_row[0] != "ab":
        return CONFIRMED, (f"From position 0, T-SQL returns {t_row[0]!r} and Spark returns {s_row[0]!r}. "
                           f"From position -1, T-SQL returns {t_row[1]!r} and Spark returns {s_row[1]!r}.")
    return REFUTED, f"T-SQL gave {t_row}, Spark gave {s_row}."


# ------------------------------------------------------------------ 9.8

def spark_9_8(spark, attempt):
    one = lambda sql: attempt(lambda: rows(spark, sql)[0][0])
    return {
        "start_0": one("SELECT overlay('abc' PLACING 'x' FROM 0 FOR 1)"),
        "start_past_end": one("SELECT overlay('abc' PLACING 'x' FROM 5 FOR 1)"),
        "negative_length": one("SELECT overlay('abc' PLACING 'x' FROM 2 FOR -1)"),
        "normal": one("SELECT overlay('abc' PLACING 'x' FROM 2 FOR 1)"),
    }


def judge_9_8(t, s):
    start_0, past_end, negative, normal = t["sets"][0][0]
    spark_values = {k: v.get("ok", "error") for k, v in s.items()}
    if start_0 is None and past_end is None and negative is None and normal == "axc" and spark_values["start_0"] is not None:
        return CONFIRMED, ("STUFF returns NULL for start 0, a start past the end and a negative length. overlay returns "
                           f"{spark_values['start_0']!r}, {spark_values['start_past_end']!r} and "
                           f"{spark_values['negative_length']!r} for the same three.")
    return REFUTED, f"T-SQL gave {(start_0, past_end, negative, normal)}, Spark gave {spark_values}."


# ------------------------------------------------------------------ 9.10

TIES_VALUES = "(VALUES (1), (2), (3), (4), (5), (5), (5), (6)) AS v(x)"


def spark_9_10(spark, attempt):
    return {
        "limit_5": attempt(lambda: len(rows(spark, f"SELECT x FROM {TIES_VALUES} ORDER BY x LIMIT 5"))),
        "limit_5_with_ties": attempt(lambda: len(rows(spark, f"SELECT x FROM {TIES_VALUES} ORDER BY x LIMIT 5 WITH TIES"))),
        "rank_window": attempt(lambda: len(rows(
            spark, f"SELECT x FROM (SELECT x, rank() OVER (ORDER BY x) AS r FROM {TIES_VALUES}) WHERE r <= 5"))),
    }


def judge_9_10(t, s):
    t_count = len(t["sets"][0])
    if t_count == 7 and s["limit_5"].get("ok") == 5 and "error" in s["limit_5_with_ties"]:
        note = "TOP 5 WITH TIES returns 7 rows; LIMIT 5 returns 5 and has no WITH TIES form."
        if s["rank_window"].get("ok") == 7:
            note += " A rank() window filtered to <= 5 returns the same 7."
        return CONFIRMED, note
    return REFUTED, f"T-SQL returned {t_count} rows; Spark: {s}."


# ------------------------------------------------------------------ 9.14

def spark_9_14(spark, attempt):
    return {
        "plain_literal": attempt(lambda: rows(spark, r"SELECT 'C:\temp\new' AS p, length('C:\temp\new') AS n")[0]),
        "raw_literal": attempt(lambda: rows(spark, r"SELECT r'C:\temp\new' AS p, length(r'C:\temp\new') AS n")[0]),
    }


def judge_9_14(t, s):
    t_len = t["sets"][0][0][1]
    plain, raw = s["plain_literal"].get("ok"), s["raw_literal"].get("ok")
    if t_len == 11 and plain and plain[1] != 11:
        note = f"T-SQL keeps the backslashes (length 11); Spark turns \\t and \\n into a tab and a newline (length {plain[1]})."
        if raw and raw[1] == 11:
            note += " A raw literal r'...' keeps them."
        return CONFIRMED, note
    return REFUTED, f"T-SQL length {t_len}; Spark: {s}."


# ------------------------------------------------------------------ extra: an inferred agreement

EMPTY_SQL = "SELECT SUM(v) AS s, AVG(v) AS a, COUNT(*) AS c, MAX(v) AS m FROM (SELECT 1 AS v WHERE 1 = 0) AS e"


def spark_empty(spark, attempt):
    return {"aggregates": attempt(lambda: rows(spark, EMPTY_SQL)[0])}


def judge_empty(t, s):
    t_row, s_row = tuple(t["sets"][0][0]), tuple(s["aggregates"].get("ok") or ())
    if t_row == s_row == (None, None, 0, None):
        return CONFIRMED, "Both return NULL for SUM, AVG and MAX and 0 for COUNT(*) over no rows."
    return REFUTED, f"T-SQL gave {t_row}, Spark gave {s_row}."


CASES = [
    dict(
        id="2.2",
        title="String keys differing only in case, in JOIN, GROUP BY, DISTINCT, IN and set operators",
        claim="T-SQL: one key. Spark: separate keys.",
        tsql=KEYS_SQL, spark=spark_2_2, judge=judge_2_2,
    ),
    dict(
        id="3.11",
        title="money / money",
        claim="T-SQL: money keeps four decimal places. Spark: DECIMAL(19, 4) division keeps more digits.",
        tsql="""SELECT CAST(10 AS money) / CAST(3 AS money) AS ten_thirds,
                       CAST(2 AS money) / CAST(3 AS money) AS two_thirds,
                       CAST(1 AS money) / CAST(3 AS money) * 3 AS third_times_three""",
        spark=spark_3_11, judge=judge_3_11,
    ),
    dict(
        id="3.12",
        title="ISNUMERIC('$')",
        claim="T-SQL: 1 for '$', '+' and '-'. Spark: no equivalent; a try_cast emulation answers differently.",
        tsql="SELECT ISNUMERIC('$'), ISNUMERIC('+'), ISNUMERIC('-'), ISNUMERIC('1e5'), ISNUMERIC(',')",
        spark=spark_3_12, judge=judge_3_12,
    ),
    dict(
        id="6.4",
        title="SCOPE_IDENTITY(), @@IDENTITY, IDENT_CURRENT",
        claim="T-SQL: the last identity value generated. Spark: no equivalent.",
        tsql="""DROP TABLE IF EXISTS dbo.d64;
                CREATE TABLE dbo.d64 (id int IDENTITY(10, 5) PRIMARY KEY, v int);
                INSERT INTO dbo.d64 (v) VALUES (1);
                INSERT INTO dbo.d64 (v) VALUES (2);
                SELECT CAST(SCOPE_IDENTITY() AS int), CAST(@@IDENTITY AS int), CAST(IDENT_CURRENT('dbo.d64') AS int)""",
        spark=spark_6_4, judge=judge_6_4,
    ),
    dict(
        id="7.2",
        title="Three inserts, the second violating a constraint, XACT_ABORT OFF, no TRY",
        claim="T-SQL: only the failing statement is rolled back and the third insert runs. "
              "Spark: an uncaught Python exception stops the script at the second.",
        # The table is read back in a later batch: the driver drops the result
        # sets of a batch in which a statement failed.
        tsql=[
            """DROP TABLE IF EXISTS dbo.d72;
               CREATE TABLE dbo.d72 (v int NOT NULL CONSTRAINT ck_d72 CHECK (v > 0));""",
            """SET XACT_ABORT OFF;
               INSERT INTO dbo.d72 (v) VALUES (1);
               INSERT INTO dbo.d72 (v) VALUES (-1);
               INSERT INTO dbo.d72 (v) VALUES (3);""",
            "SELECT v FROM dbo.d72 ORDER BY v",
        ],
        all_batches=True,
        spark=spark_7_2, judge=judge_7_2,
    ),
    dict(
        id="7.6",
        title="What BEGIN TRY ... BEGIN CATCH catches",
        claim="T-SQL: not warnings, and not compile errors at the same level. "
              "Spark: a Python except catches every exception, including analysis errors.",
        # Each probe is a procedure that logs how far it got, read back in the last batch.
        tsql=[
            """DROP PROCEDURE IF EXISTS dbo.d76_warning, dbo.d76_missing, dbo.d76_divide, dbo.d76_inner, dbo.d76_outer;
               DROP TABLE IF EXISTS dbo.d76_log;
               CREATE TABLE dbo.d76_log (n int IDENTITY PRIMARY KEY, step varchar(80) NOT NULL);""",
            """CREATE PROCEDURE dbo.d76_warning AS
               BEGIN TRY
                   RAISERROR('just a warning', 10, 1);
                   INSERT INTO dbo.d76_log (step) VALUES ('warning: not caught');
               END TRY
               BEGIN CATCH
                   INSERT INTO dbo.d76_log (step) VALUES ('warning: caught');
               END CATCH""",
            """CREATE PROCEDURE dbo.d76_missing AS
               BEGIN
                   BEGIN TRY
                       SELECT * FROM dbo.no_such_table_76;
                       INSERT INTO dbo.d76_log (step) VALUES ('missing table, same level: not caught');
                   END TRY
                   BEGIN CATCH
                       INSERT INTO dbo.d76_log (step) VALUES ('missing table, same level: caught');
                   END CATCH;
                   INSERT INTO dbo.d76_log (step) VALUES ('missing table, same level: carried on after the TRY block');
               END""",
            """CREATE PROCEDURE dbo.d76_divide AS
               BEGIN TRY
                   DECLARE @x int = 1 / 0;
                   INSERT INTO dbo.d76_log (step) VALUES ('divide by zero: not caught');
               END TRY
               BEGIN CATCH
                   INSERT INTO dbo.d76_log (step) VALUES ('divide by zero: caught');
               END CATCH""",
            "CREATE PROCEDURE dbo.d76_inner AS SELECT * FROM dbo.no_such_table_76",
            """CREATE PROCEDURE dbo.d76_outer AS
               BEGIN TRY
                   EXEC dbo.d76_inner;
                   INSERT INTO dbo.d76_log (step) VALUES ('missing table, one level down: not caught');
               END TRY
               BEGIN CATCH
                   INSERT INTO dbo.d76_log (step) VALUES ('missing table, one level down: caught by the caller');
               END CATCH""",
            "EXEC dbo.d76_warning",
            "EXEC dbo.d76_missing",
            "EXEC dbo.d76_divide",
            "EXEC dbo.d76_outer",
            "SELECT step FROM dbo.d76_log ORDER BY n",
        ],
        all_batches=True, spark=spark_7_6, judge=judge_7_6,
    ),
    dict(
        id="8.1",
        title="MERGE ... ON t.code = s.code with string keys",
        claim="T-SQL: keys match ignoring case and trailing spaces, so the row is updated. "
              "Spark: no match, so a second row is inserted.",
        tsql="""DROP TABLE IF EXISTS dbo.d81_t; DROP TABLE IF EXISTS dbo.d81_s;
                CREATE TABLE dbo.d81_t (code varchar(10) NOT NULL, v int NOT NULL);
                CREATE TABLE dbo.d81_s (code varchar(10) NOT NULL, v int NOT NULL);
                INSERT INTO dbo.d81_t VALUES ('AB01', 1);
                INSERT INTO dbo.d81_s VALUES ('ab01  ', 2);
                MERGE dbo.d81_t AS t USING dbo.d81_s AS s ON t.code = s.code
                WHEN MATCHED THEN UPDATE SET v = s.v
                WHEN NOT MATCHED THEN INSERT (code, v) VALUES (s.code, s.v);
                SELECT code, v FROM dbo.d81_t ORDER BY v""",
        spark=spark_8_1, judge=judge_8_1,
    ),
    dict(
        id="8.3",
        title="MERGE ... OUTPUT $action, inserted.*, deleted.*",
        claim="T-SQL: one row per row inserted, updated or deleted. Spark: no OUTPUT clause.",
        tsql="""DROP TABLE IF EXISTS dbo.d83_t;
                CREATE TABLE dbo.d83_t (k int PRIMARY KEY, v int);
                INSERT INTO dbo.d83_t VALUES (1, 10);
                MERGE dbo.d83_t AS t USING (VALUES (1, 11), (2, 20)) AS s(k, v) ON t.k = s.k
                WHEN MATCHED THEN UPDATE SET v = s.v
                WHEN NOT MATCHED THEN INSERT (k, v) VALUES (s.k, s.v)
                OUTPUT $action, inserted.k, deleted.v, inserted.v;""",
        spark=spark_8_3, judge=judge_8_3,
    ),
    dict(
        id="8.4",
        title="A MERGE target that has triggers",
        claim="T-SQL: AFTER triggers fire for each action. Spark: no triggers, so their side effects do not happen.",
        tsql=[
            """DROP TABLE IF EXISTS dbo.d84_log; DROP TABLE IF EXISTS dbo.d84_t;
               CREATE TABLE dbo.d84_t (k int PRIMARY KEY, v int);
               CREATE TABLE dbo.d84_log (what varchar(10), n int);""",
            "CREATE TRIGGER dbo.d84_ins ON dbo.d84_t AFTER INSERT AS INSERT INTO dbo.d84_log SELECT 'insert', COUNT(*) FROM inserted;",
            "CREATE TRIGGER dbo.d84_upd ON dbo.d84_t AFTER UPDATE AS INSERT INTO dbo.d84_log SELECT 'update', COUNT(*) FROM inserted;",
            """INSERT INTO dbo.d84_t VALUES (1, 10);
               DELETE FROM dbo.d84_log;
               MERGE dbo.d84_t AS t USING (VALUES (1, 11), (2, 20)) AS s(k, v) ON t.k = s.k
               WHEN MATCHED THEN UPDATE SET v = s.v
               WHEN NOT MATCHED THEN INSERT (k, v) VALUES (s.k, s.v);
               SELECT what, n FROM dbo.d84_log ORDER BY what""",
        ],
        spark=spark_8_4, judge=judge_8_4,
    ),
    dict(
        id="8.5",
        title="MERGE with one unconditional WHEN MATCHED THEN DELETE and several source rows per target row",
        claim="T-SQL: not stated for the delete-only case. Spark: allowed.",
        tsql="""DROP TABLE IF EXISTS dbo.d85_t;
                CREATE TABLE dbo.d85_t (k int, v int);
                INSERT INTO dbo.d85_t VALUES (1, 10), (2, 20);
                MERGE dbo.d85_t AS t USING (VALUES (1), (1)) AS s(k) ON t.k = s.k
                WHEN MATCHED THEN DELETE;
                SELECT k FROM dbo.d85_t ORDER BY k""",
        spark=spark_8_5, judge=judge_8_5,
    ),
    dict(
        id="9.4",
        title="SUBSTRING('abcdef', 0, 3)",
        claim="T-SQL: 'ab'. Spark: not documented for position 0.",
        tsql="SELECT SUBSTRING('abcdef', 0, 3), SUBSTRING('abcdef', -1, 3), SUBSTRING('abcdef', 1, 3)",
        spark=spark_9_4, judge=judge_9_4,
    ),
    dict(
        id="9.8",
        title="STUFF('abc', 0, 1, 'x')",
        claim="T-SQL: NULL when the start is zero, negative or past the end, or the length is negative. "
              "Spark: overlay's out-of-range behaviour is not documented.",
        tsql="SELECT STUFF('abc', 0, 1, 'x'), STUFF('abc', 5, 1, 'x'), STUFF('abc', 2, -1, 'x'), STUFF('abc', 2, 1, 'x')",
        spark=spark_9_8, judge=judge_9_8,
    ),
    dict(
        id="9.10",
        title="SELECT TOP 5 WITH TIES ... ORDER BY x",
        claim="T-SQL: also returns rows that tie with the last row. Spark: LIMIT has no ties option.",
        tsql=f"SELECT TOP 5 WITH TIES x FROM {TIES_VALUES} ORDER BY x",
        spark=spark_9_10, judge=judge_9_10,
    ),
    dict(
        id="9.14",
        title=r"'C:\temp\new' inside embedded SQL text",
        claim="T-SQL: backslash is an ordinary character. Spark: backslash escapes in SQL string literals.",
        tsql=r"SELECT 'C:\temp\new' AS p, LEN('C:\temp\new') AS n",
        spark=spark_9_14, judge=judge_9_14,
    ),
]

# Not one of the 14: the research inferred that the two sides agree here.
EXTRA_CASES = [
    dict(
        id="10 (empty input)",
        title="SUM, AVG, MAX and COUNT(*) over no rows",
        claim="Inferred to agree: NULL for SUM, AVG and MAX, 0 for COUNT(*).",
        tsql=EMPTY_SQL, spark=spark_empty, judge=judge_empty,
    ),
]
