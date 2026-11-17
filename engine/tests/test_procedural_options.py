"""SET options in generated code (issue #117, spec section 6): `SET NOCOUNT` generates, the option a procedure or a trigger
changes is back to its old value when it ends, and a batch's change lasts for the session, as on SQL Server, which is the
oracle for each of these (`@@OPTIONS & 512` is NOCOUNT). `SET IDENTITY_INSERT` waits for the writes, and an option lowering
does not emit stays an opaque fragment with its reason."""
import pytest

import migrt_spark as lib
from generation_helpers import generate_text
from lowering_helpers import parameter
from migrt.targets.pyspark import generator, untranslatable
from test_lowering_corpus import lowered_modules
from test_generation_corpus import generated
from test_procedural_generation import PRELUDES, on_sql_server, on_target, procedure

NOCOUNT = 512
# The corpus procedures and triggers that generate without manual work. None yet: what still stops each is a statement kind a
# later ticket builds (the failure message of the corpus test lists them). The ticket that makes the first one whole adds it
# here, and each ticket after it adds its own.
WHOLE: list[str] = []


def nocount_on_sql_server(duo, batches: list[str]) -> list[int]:
    """The NOCOUNT bit after each batch, in one SQL Server session that carries the setting from batch to batch."""
    return [duo.tsql(batch + "; SELECT @@OPTIONS & 512 AS o")[0]["o"] for batch in batches]


# ---------------------------------------------------------------- the library

def test_an_option_set_in_a_scope_is_back_when_the_scope_ends_and_one_set_outside_stays(duo):
    ss = duo.session()
    assert ss.options["NOCOUNT"] is False

    with ss.scope("p", procedure_call=True):
        ss.set_option("NOCOUNT", True)
        assert ss.options["NOCOUNT"] is True
    assert ss.options["NOCOUNT"] is False

    ss.set_option("NOCOUNT", True)
    with ss.scope("q"):
        ss.set_option("NOCOUNT", False)
    assert ss.options["NOCOUNT"] is True


def test_an_option_is_restored_when_the_scope_ends_with_an_error(duo):
    ss = duo.session()

    with pytest.raises(lib.TsqlError):
        with ss.scope("p"):
            ss.set_option("NOCOUNT", True)
            raise ss.fail("divide_by_zero")

    assert ss.options["NOCOUNT"] is False


def test_an_option_the_library_does_not_know_is_refused_not_ignored(duo):
    with pytest.raises(KeyError, match="ANSI_NULLS"):
        duo.session().set_option("ANSI_NULLS", True)


# ---------------------------------------------------------------- generated code against SQL Server

def test_set_nocount_in_a_procedure_generates_and_the_option_is_back_when_it_returns(corpus_snapshot, duo):
    sql = "CREATE PROCEDURE sales.uspTest AS BEGIN\nSET NOCOUNT ON;\nRETURN 3;\nEND"
    generated_file, function = procedure(corpus_snapshot, sql)
    ss = duo.session()

    result = function(ss)

    assert generated_file.file.manual_items == [] and not generated_file.file.fails_when_called
    assert result.return_code == 3
    duo.tsql(sql.replace("sales.", "{s}."))
    assert nocount_on_sql_server(duo, ["EXEC {s}.uspTest"]) == [0]
    assert ss.options["NOCOUNT"] is False


def test_set_nocount_off_in_a_procedure_leaves_a_sessions_setting_as_it_was(corpus_snapshot, duo):
    sql = "CREATE PROCEDURE sales.uspTest AS BEGIN\nSET NOCOUNT OFF;\nRETURN 0;\nEND"
    _, function = procedure(corpus_snapshot, sql)
    duo.tsql(sql.replace("sales.", "{s}."))
    ss = duo.session()
    ss.set_option("NOCOUNT", True)

    function(ss)

    assert nocount_on_sql_server(duo, ["SET NOCOUNT ON; EXEC {s}.uspTest"]) == [NOCOUNT]
    assert ss.options["NOCOUNT"] is True


def test_set_nocount_in_a_batch_lasts_for_the_session(corpus_snapshot, duo):
    script = "SET NOCOUNT ON;\nGO\nDECLARE @n int = 1;"
    file = generate_text(corpus_snapshot, script, kind="query_batch", name="options")
    assert file.layer1.passed, file.layer1.failures
    namespace: dict = {}
    exec(compile(file.text, "options.py", "exec"), namespace)
    ss = duo.session()

    namespace["batch_1"](ss)
    after_first = ss.options["NOCOUNT"]
    namespace["batch_2"](ss)

    assert file.file.manual_items == []
    assert (after_first, ss.options["NOCOUNT"]) == (True, True)
    assert nocount_on_sql_server(duo, ["SET NOCOUNT ON", "DECLARE @n int = 1"]) == [NOCOUNT, NOCOUNT]


def test_set_nocount_is_recorded_in_the_report_because_generated_code_sends_no_row_counts(corpus_snapshot):
    sql = "CREATE PROCEDURE sales.uspTest AS BEGIN\nSET NOCOUNT ON;\nSET NOCOUNT OFF;\nSET NOCOUNT ON;\nEND"
    file = generate_text(corpus_snapshot, sql, kind="procedure", name="uspTest")

    notes = [a for a in file.file.assumptions if "NOCOUNT" in a]
    assert len(notes) == 1                                    # once for the procedure, not for each statement
    assert "no row counts" in notes[0] and "#" not in notes[0]
    assert "SQL Server" not in notes[0]                       # the target half speaks of the target


@pytest.mark.parametrize("reads", ["@@ROWCOUNT", "@@ERROR"])
@pytest.mark.parametrize("prelude", sorted(PRELUDES))
@pytest.mark.parametrize("statement", ["SET NOCOUNT ON;", "SET NOCOUNT OFF;"])
def test_set_nocount_leaves_in_rowcount_and_error_what_sql_server_does(corpus_snapshot, duo, statement, prelude, reads):
    sql = f"CREATE PROCEDURE sales.uspTest @pre int = 0 AS BEGIN\n{statement}\nRETURN {reads};\nEND"
    file, function = procedure(corpus_snapshot, sql, [parameter("@pre")])
    statement_text, (rows, error) = PRELUDES[prelude]
    expected = on_sql_server(duo, sql, {}, prelude=statement_text, expected_errors=(50000,) if error else ())
    ss = duo.session()
    ss.rowcount, ss.at_error = rows, error

    assert on_target(ss, function, {}) == expected
    assert file.file.manual_items == []


# ---------------------------------------------------------------- what is not generated yet

def test_set_identity_insert_waits_for_the_writes(corpus_snapshot):
    sql = "CREATE PROCEDURE sales.uspTest AS BEGIN\nSET IDENTITY_INSERT sales.[Order] ON;\nRETURN 0;\nEND"
    file = generate_text(corpus_snapshot, sql, kind="procedure", name="uspTest")

    assert file.layer1.passed, file.layer1.failures
    [item] = file.file.manual_items
    assert item.waits_for == ("#118",) and "#" not in item.reason and "IDENTITY_INSERT" in item.reason
    assert file.file.fails_when_called


def test_an_option_lowering_does_not_emit_stays_an_opaque_fragment_with_its_reason(corpus_snapshot):
    sql = "CREATE PROCEDURE sales.uspTest AS BEGIN\nSET ANSI_PADDING OFF;\nRETURN 0;\nEND"
    file = generate_text(corpus_snapshot, sql, kind="procedure", name="uspTest")

    [item] = file.file.manual_items
    assert "ANSI_PADDING" in item.reason and item.waits_for == (untranslatable.LOWERING_ASSIST,)
    assert "#" not in item.reason


# ---------------------------------------------------------------- the corpus

def test_no_corpus_procedure_or_trigger_is_stopped_by_set_nocount_any_more(corpus_snapshot):
    """The corpus procedures and triggers, with what still stops each: SET NOCOUNT is not on the list. A procedure that
    SET NOCOUNT alone stopped now generates whole."""
    stopping: dict[str, set[str]] = {}
    for identity, artefact, _ in lowered_modules(corpus_snapshot):
        if artefact.artefact_type not in ("procedure", "trigger"):
            continue
        file, layer1 = generated(corpus_snapshot, artefact, artefact.artefact_type)
        assert layer1.passed, (identity, layer1.failures)
        nodes = {n.id: n for n in artefact.nodes()}
        stopping[identity] = {nodes[m.node].kind for m in file.manual_items}
        nocount = [nodes[m.node] for m in file.manual_items
                   if nodes[m.node].kind == "set_session_option" and nodes[m.node].attrs["option"] == "NOCOUNT"]
        assert nocount == [], identity
    assert len(stopping) >= 50
    whole = sorted(i for i, kinds in stopping.items() if not kinds)
    assert whole == WHOLE, (f"the corpus procedures and triggers that generate whole changed: update WHOLE. Now {whole}; "
                            f"the statement kinds still stopping the rest: {sorted({k for s in stopping.values() for k in s})}")


# ---------------------------------------------------------------- what each kind waits for

@pytest.mark.parametrize("kind,ticket", [
    ("insert_rows", "#118"), ("update_rows", "#118"), ("delete_rows", "#118"), ("call_procedure", "#118"),
    ("begin_transaction", "#119"), ("try_catch", "#119"), ("raise_error", "#119"), ("emit_message", "#119"),
    ("set_abort_on_error", "#119"), ("create_temp_table", "#120"), ("assign_from_rows", "#120"),
    ("declare_row_set_variable", "#120"), ("merge_rows", "#121"), ("return_changed_rows", "#121"),
    ("read_changed_rows", "#122"), ("declare_cursor", "#124"), ("fetch_cursor", "#124"),
])
def test_a_statement_not_generated_yet_waits_for_the_ticket_that_builds_it(kind, ticket):
    assert untranslatable.waits_for_kind(kind, generator.handled_kinds()) == (ticket,)


def test_an_option_a_trigger_sets_is_back_when_the_write_wrapper_has_run_it(duo):
    """The wrapper fires a trigger inside a scope of its own, so the option a trigger sets does not leak into the session, as on
    SQL Server (compared)."""
    from support_harness import C

    def trigger(ss, inserted, deleted):
        ss.set_option("NOCOUNT", True)

    duo.table("T", [C("Id", "int")])
    duo.tsql("CREATE TRIGGER {s}.trT ON {s}.T AFTER INSERT AS SET NOCOUNT ON")
    duo.with_triggers("T", lib.Trigger("trT", "after", frozenset({"insert"}), trigger))
    ss = duo.session()

    ss.insert_values(duo.target("T"), [{"Id": 1}])

    assert nocount_on_sql_server(duo, ["INSERT {s}.T VALUES (1)"]) == [0]
    assert ss.options["NOCOUNT"] is False
