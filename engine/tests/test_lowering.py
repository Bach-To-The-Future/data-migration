"""Lowering of queries and expressions (issue #42, spec sections 4 and 5), at the lowering
library interface: SQL text in, the intermediate representation out, checked by the checker.

Expected values come from T-SQL's documented rules (data type precedence, the precision and
scale formulas, collation precedence), not from the code under test."""
import pytest

from migrt.core import checker
from migrt.core.ir import Node
from migrt.sources.sqlserver import lowering

from lowering_helpers import kinds, lower_text, of_kind, query


def lower(snapshot, select: str, **kw):
    return lower_text(snapshot, f"CREATE VIEW sales.vTest AS\n{select}", **kw)[0]


def chain(node: Node) -> list[str]:
    """The plan steps from a step down its `input` children."""
    out = [node.kind]
    while node.children_with("input"):
        node = node.child("input")
        out.append(node.kind)
    return out


def checked(artefact, reader) -> list:
    return checker.check(artefact, lowering.columns_of(reader))


def lowered_and_checked(snapshot, select: str, **kw):
    artefact, reader = lower_text(snapshot, f"CREATE VIEW sales.vTest AS\n{select}", **kw)
    assert checked(artefact, reader) == []
    return artefact


# ---------------------------------------------------------------- queries as a logical plan

def test_a_query_lowers_to_steps_in_the_sources_order_keeping_its_names(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot, "SELECT o.OrderId AS Id FROM sales.[Order] AS o WHERE o.CustomerId = 1")

    plan = query(artefact)
    assert chain(plan) == ["project", "filter", "read_table"]
    [read] = of_kind(artefact, "read_table")
    assert read.attrs["table"] == "RetailSample.sales.Order" and read.attrs["alias"] == "o"
    assert [o.attrs["output_name"] for o in plan.children_with("output")] == ["Id"]


def test_distinct_sort_and_top_are_steps_after_the_projection_in_t_sqls_order(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot,
        "SELECT DISTINCT TOP (5) WITH TIES o.Status FROM sales.[Order] AS o ORDER BY o.Status DESC")

    plan = query(artefact)
    assert chain(plan) == ["limit", "sort", "distinct", "project", "read_table"]
    assert plan.attrs == {"percent": False, "with_ties": True}
    assert plan.child("count").attrs["value"] == 5
    [key] = plan.child("input").children_with("order")
    assert key.kind == "column" and key.attrs["direction"] == "descending"


def test_offset_fetch_is_skip_and_limit(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot,
        "SELECT o.OrderId FROM sales.[Order] AS o ORDER BY o.OrderId OFFSET 10 ROWS FETCH NEXT 5 ROWS ONLY")

    plan = query(artefact)
    assert chain(plan) == ["skip_and_limit", "sort", "project", "read_table"]
    assert (plan.child("skip").attrs["value"], plan.child("count").attrs["value"]) == (10, 5)


def test_group_by_having_and_aggregates_in_one_aggregate_step(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot,
        "SELECT o.CustomerId, COUNT(*) AS Orders FROM sales.[Order] AS o "
        "GROUP BY o.CustomerId HAVING COUNT(*) > 2")

    plan = query(artefact)
    assert plan.kind == "aggregate" and [k.kind for k in plan.children_with("key")] == ["column"]
    assert plan.child("having").kind == "compare"
    assert [o.kind for o in plan.children_with("output")] == ["column", "count_rows"]


def test_set_operations_keep_both_inputs(corpus_snapshot):
    for sql, kind, extra in (("UNION ALL", "union", {"all": True}), ("UNION", "union", {"all": False}),
                             ("INTERSECT", "intersect", {}), ("EXCEPT", "except", {})):
        artefact = lowered_and_checked(
            corpus_snapshot, f"SELECT o.OrderId FROM sales.[Order] AS o {sql} SELECT a.OrderId FROM sales.OrderArchive AS a")
        plan = query(artefact)
        assert plan.kind == kind and plan.attrs == extra
        assert [plan.child("left").kind, plan.child("right").kind] == ["project", "project"]


def test_comma_from_is_a_cross_join_and_apply_is_a_lateral_join(corpus_snapshot):
    cross = lowered_and_checked(
        corpus_snapshot, "SELECT o.OrderId, c.Name FROM sales.[Order] AS o, ref.Customer AS c WHERE o.CustomerId = c.CustomerId")
    [join] = of_kind(cross, "join")
    assert join.attrs["kind"] == "cross" and not join.children_with("condition")

    lateral = lowered_and_checked(
        corpus_snapshot,
        "SELECT o.OrderId, f.Sku FROM sales.[Order] AS o CROSS APPLY sales.fnOrderLines(o.OrderId) AS f")
    [apply] = of_kind(lateral, "lateral_join")
    assert apply.attrs["kind"] == "inner"
    [call] = of_kind(lateral, "call_table_function")
    assert call.attrs["function"] == "RetailSample.sales.fnOrderLines" and call.attrs["alias"] == "f"
    assert call.child("argument").kind == "column"


def test_derived_tables_and_common_table_expressions_are_named_queries(corpus_snapshot):
    derived = lowered_and_checked(
        corpus_snapshot, "SELECT d.OrderId FROM (SELECT o.OrderId FROM sales.[Order] AS o) AS d")
    [named] = of_kind(derived, "named_query")
    assert named.attrs["name"] == "d" and named.child("query").kind == "project"

    cte = lowered_and_checked(
        corpus_snapshot,
        "WITH recent AS (SELECT o.OrderId FROM sales.[Order] AS o) SELECT r.OrderId FROM recent AS r")
    [definition] = of_kind(cte, "named_query")
    assert definition.attrs["name"] == "recent" and definition.role == "definition"
    [read] = [n for n in cte.nodes() if n.kind == "read_table" and n.attrs.get("named_query")]
    assert read.attrs["named_query"] == "recent" and read.attrs["alias"] == "r"


def test_subqueries_see_the_outer_query_and_keep_their_own_kind(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot,
        "SELECT c.CustomerId FROM ref.Customer AS c "
        "WHERE EXISTS (SELECT 1 FROM sales.[Order] AS o WHERE o.CustomerId = c.CustomerId) "
        "AND c.CustomerId IN (SELECT a.CustomerId FROM sales.OrderArchive AS a) "
        "AND c.CreditLimit > (SELECT MAX(l.UnitPrice) FROM sales.OrderLine AS l)")

    assert {"exists", "in_query", "scalar_query"} <= set(kinds(query(artefact)))
    [scalar] = of_kind(artefact, "scalar_query")
    assert scalar.value_type.name == "money"


def test_star_expands_to_the_catalogs_columns_in_order(corpus_snapshot):
    artefact = lowered_and_checked(corpus_snapshot, "SELECT * FROM sales.OrderArchive")

    assert [o.attrs["output_name"] for o in query(artefact).children_with("output")] == [
        "OrderId", "CustomerId", "OrderDate", "Status", "TotalAmount", "ArchivedAt"]


def test_a_window_function_carries_its_partition_order_and_explicit_frame(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot,
        "SELECT RANK() OVER (PARTITION BY l.OrderId ORDER BY l.Quantity DESC) AS R, "
        "SUM(l.Quantity) OVER () AS Total FROM sales.OrderLine AS l")

    ranked, total = of_kind(artefact, "window")
    assert ranked.child("function").kind == "rank" and ranked.child("function").attrs == {"dense": False}
    assert ranked.child("partition").kind == "column" and ranked.child("order").attrs["direction"] == "descending"
    assert ranked.attrs["frame"] == {"unit": "range", "start": "unbounded_preceding", "end": "current_row"}
    assert total.attrs["frame"] == {"unit": "partition", "start": "unbounded_preceding", "end": "unbounded_following"}
    assert ranked.value_type.name == "integer" and ranked.value_type.int_param("bits") == 64


# ---------------------------------------------------------------- types

def output(artefact, name: str) -> Node:
    [node] = [o for o in query(artefact).children_with("output") if o.attrs["output_name"] == name]
    return node


@pytest.mark.parametrize("expression, operation, type_name, params", [
    ("o.Status / 2", "integer_divide", "integer", {"bits": 32}),        # tinyint / int literal -> int
    ("o.Status + o.Status", "add", "integer", {"bits": 8}),
    ("l.Quantity * l.UnitPrice", "money_multiply", "money", {"precision": 19}),
    ("l.UnitPrice / l.Quantity", "money_divide", "money", {"precision": 19}),
    ("p.WeightKg * 2", "multiply", "decimal", {"precision": 20, "scale": 3}),     # (9,3) * (10,0): p1+p2+1, s1+s2
    ("p.WeightKg + p.WeightKg", "add", "decimal", {"precision": 10, "scale": 3}),
    ("p.WeightKg / p.WeightKg", "decimal_divide", "decimal", {"precision": 22, "scale": 13}),    # p1-s1+s2+max(6,s1+p2+1), max(6,s1+p2+1)
    ("2147483648", "literal", "decimal", {"precision": 10, "scale": 0}),
    ("12.50", "literal", "decimal", {"precision": 4, "scale": 2}),
    ("-o.OrderId", "negate", "integer", {"bits": 32}),
])
def test_expressions_get_their_t_sql_types_from_the_catalog(corpus_snapshot, expression, operation, type_name, params):
    artefact = lowered_and_checked(
        corpus_snapshot,
        "SELECT X = " + expression + " FROM sales.[Order] AS o, sales.OrderLine AS l, ref.Product AS p")

    node = output(artefact, "X")
    assert node.kind == operation, node.kind
    assert node.value_type.name == type_name and params.items() <= dict(node.value_type.params).items()


def test_operands_of_different_types_get_the_conversion_t_sql_makes(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot, "SELECT o.OrderId FROM sales.[Order] AS o WHERE o.Status <> 9")

    [compare] = of_kind(artefact, "compare")
    first, second = compare.children_with("argument")
    assert first.kind == "implicit_convert" and first.value_type.int_param("bits") == 32
    assert first.child("argument").kind == "column" and second.kind == "literal"


def test_string_comparisons_carry_the_rule_from_collation_precedence(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot,
        "SELECT c.CustomerId FROM ref.Customer AS c WHERE c.Region = 'emea' AND c.Name <> N'x'")
    ci_compare, ci_wide = of_kind(artefact, "compare")
    rule = ci_compare.attrs["rule"]
    # The column's collation is implicit and beats the literal's coercible default.
    assert rule["case_sensitive"] is False and rule["accent_sensitive"] is True
    assert rule["ignore_trailing_spaces"] is True and rule["origin"] == "column"
    assert rule["collation"] == "SQL_Latin1_General_CP1_CI_AS"

    cs = lowered_and_checked(corpus_snapshot, "SELECT p.ProductId FROM ref.Product AS p WHERE p.Sku = 'a-1'")
    [compare] = of_kind(cs, "compare")
    assert compare.attrs["rule"]["case_sensitive"] is True
    assert compare.attrs["rule"]["collation"] == "Latin1_General_CS_AS"


def test_two_different_implicit_collations_are_a_conflict_kept_as_an_opaque_fragment(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot, "SELECT p.ProductId FROM ref.Product AS p WHERE p.Sku = p.Category")

    [fragment] = of_kind(artefact, "opaque_fragment")
    assert "collation" in fragment.attrs["reason"] and "conflict" in fragment.attrs["reason"]
    assert "p.Sku = p.Category" in fragment.attrs["text"]


def test_every_string_comparing_operation_names_its_rule(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot,
        "SELECT NULLIF(c.Region, 'x') AS N, MIN(c.Name) AS Lowest FROM ref.Customer AS c "
        "WHERE c.Region IN ('a', 'b') AND c.Name LIKE 'A%' AND c.Name BETWEEN 'a' AND 'm' "
        "GROUP BY c.Region")
    for kind in ("null_if_equal", "minimum", "in_list", "like", "between"):
        [node] = of_kind(artefact, kind)
        assert node.attrs["rule"]["collation"] == "SQL_Latin1_General_CP1_CI_AS", kind


def test_case_coalesce_and_isnull_are_typed_by_precedence(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot,
        "SELECT A = CASE WHEN o.Status = 1 THEN o.OrderId ELSE NULL END, "
        "B = COALESCE(o.Status, o.OrderId, 0), C = ISNULL(o.Note, N'none') FROM sales.[Order] AS o")

    assert output(artefact, "A").kind == "case" and output(artefact, "A").value_type.int_param("bits") == 32
    assert output(artefact, "B").kind == "first_non_null" and output(artefact, "B").value_type.int_param("bits") == 32
    assert [c.kind for c in output(artefact, "B").children_with("argument")] == ["implicit_convert", "column", "literal"]
    c = output(artefact, "C")
    assert c.kind == "null_replacement" and c.value_type.name == "string" and c.value_type.param("length") == 200


def test_cast_and_convert_record_their_target_and_style(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot,
        "SELECT A = CAST(o.OrderId AS varchar(12)), B = CONVERT(varchar(10), o.OrderDate, 112), "
        "C = TRY_CONVERT(int, o.Note) FROM sales.[Order] AS o")

    a, b, c = (output(artefact, n) for n in "ABC")
    assert (a.kind, a.value_type.param("length"), a.value_type.param("unit")) == ("convert", 12, "bytes")
    assert (b.kind, b.attrs["style"]) == ("convert_with_style", 112)
    assert (c.kind, c.value_type.name) == ("try_convert", "integer")


def test_builtin_functions_lower_to_their_one_operation_with_catalog_types(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot,
        "SELECT A = LEN(c.Name), B = UPPER(c.Name), C = DATEDIFF(day, o.OrderDate, o.ShippedAt), "
        "D = DATEADD(month, 1, o.OrderDate), E = YEAR(o.OrderDate), F = CHARINDEX('@', c.Email), "
        "G = SUBSTRING(c.Email, 2, 3), H = CONCAT(c.Name, c.Email), I = c.Name + N'!' "
        "FROM sales.[Order] AS o JOIN ref.Customer AS c ON c.CustomerId = o.CustomerId")

    expected = {"A": ("length_without_trailing_spaces", "integer"), "B": ("upper", "string"),
                "C": ("date_diff_boundaries", "integer"), "D": ("date_add", "local_timestamp"),
                "E": ("date_part", "integer"), "F": ("position_of", "integer"), "G": ("substring", "string"),
                "H": ("concat_null_as_empty", "string"), "I": ("concat_null_propagating", "string")}
    for name, (operation, type_name) in expected.items():
        node = output(artefact, name)
        assert (node.kind, node.value_type.name) == (operation, type_name), name
    assert output(artefact, "C").attrs["part"] == "day" and output(artefact, "E").attrs["part"] == "year"
    assert output(artefact, "F").attrs["rule"]["collation"]


def test_a_user_defined_scalar_function_call_is_typed_from_the_catalog(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot, "SELECT L = ref.fnFormatCustomerLabel(c.CustomerCode, c.Name, c.Email) FROM ref.Customer AS c")

    call = output(artefact, "L")
    assert call.kind == "call_scalar_function" and call.attrs["function"] == "RetailSample.ref.fnFormatCustomerLabel"
    assert call.value_type.name == "string" and call.value_type.param("length") == 200
    assert "RetailSample.ref.fnFormatCustomerLabel" in artefact.dependencies


# ---------------------------------------------------------------- more shapes, each lowering with nothing opaque

SHAPES = {
    "like with escape": "SELECT c.Name FROM ref.Customer AS c WHERE c.Name LIKE 'A!%%' ESCAPE '!'",
    "distinct": "SELECT DISTINCT c.Region FROM ref.Customer AS c",
    "simple case": "SELECT R = CASE c.Region WHEN 'EMEA' THEN 1 WHEN 'APAC' THEN 2 ELSE 0 END FROM ref.Customer AS c",
    "iif": "SELECT A = IIF(c.IsActive = 1, N'y', N'n') FROM ref.Customer AS c",
    "values": "SELECT x.A FROM (VALUES (1, 'a'), (2, 'b')) AS x (A, B)",
    "no from": "SELECT 1 AS One",
    "outer apply": "SELECT c.Name, t.N FROM ref.Customer AS c OUTER APPLY (SELECT COUNT(*) AS N FROM sales.[Order] AS o "
                   "WHERE o.CustomerId = c.CustomerId) AS t",
    "ordinal order by": "SELECT o.OrderId, o.Status FROM sales.[Order] AS o UNION ALL SELECT a.OrderId, a.Status "
                        "FROM sales.OrderArchive AS a ORDER BY 1 OFFSET 0 ROWS",
    "bit and date": "SELECT c.CustomerId FROM ref.Customer AS c WHERE c.IsActive = 1 AND c.CreatedAt >= '2020-01-01'",
    "distinct count": "SELECT COUNT(DISTINCT c.Region) AS N, STDEV(c.CreditLimit) AS S FROM ref.Customer AS c",
    "framed window": "SELECT SUM(l.Quantity) OVER (PARTITION BY l.OrderId ORDER BY l.OrderLineId ROWS BETWEEN 1 PRECEDING "
                     "AND CURRENT ROW) AS Running FROM sales.OrderLine AS l",
    "negated predicates": "SELECT c.CustomerId FROM ref.Customer AS c WHERE c.Name NOT LIKE 'A%' AND c.CustomerId NOT IN "
                          "(SELECT a.CustomerId FROM sales.OrderArchive AS a) AND c.CustomerId NOT BETWEEN 1 AND 5",
    "scalar subquery item": "SELECT c.CustomerId, Spent = (SELECT SUM(o.TotalAmount) FROM sales.[Order] AS o "
                            "WHERE o.CustomerId = c.CustomerId) FROM ref.Customer AS c",
    "numeric functions": "SELECT ABS(o.TotalAmount) AS A, ROUND(l.UnitPrice, 1) AS R, CEILING(p.WeightKg) AS C "
                         "FROM sales.[Order] AS o, sales.OrderLine AS l, ref.Product AS p",
    "a view reading a view": "SELECT t.OrderId, t.LineCount FROM sales.vOrderTotals AS t WHERE t.Units > 1",
    "string functions": "SELECT LTRIM(RTRIM(c.Name)) AS T, REPLACE(c.Email, '@', '#') AS R, LEFT(c.Name, 3) AS L, "
                        "REPLICATE('x', 3) AS X, STUFF(c.Name, 1, 1, 'z') AS S FROM ref.Customer AS c",
    "date functions": "SELECT D = DATEPART(week, o.OrderDate), N = DATENAME(month, o.OrderDate), "
                      "E = EOMONTH(o.OrderDate), G = GETDATE() FROM sales.[Order] AS o",
}


@pytest.mark.parametrize("select", SHAPES.values(), ids=SHAPES.keys())
def test_common_query_shapes_lower_completely_and_pass_the_checker(corpus_snapshot, select):
    artefact = lowered_and_checked(corpus_snapshot, select.replace("%%", "%"))

    assert of_kind(artefact, "opaque_fragment") == []


def test_a_view_column_list_renames_the_outputs(corpus_snapshot):
    artefact, reader = lower_text(
        corpus_snapshot, "CREATE VIEW sales.vTest (Id, Who) AS SELECT o.OrderId, o.CustomerId FROM sales.[Order] AS o "
                         "ORDER BY o.OrderId OFFSET 0 ROWS")

    assert checked(artefact, reader) == []
    assert [o.attrs["output_name"] for o in of_kind(artefact, "project")[0].children_with("output")] == ["Id", "Who"]
    [key] = of_kind(artefact, "sort")[0].children_with("order")
    assert key.attrs["column"] == "OrderId"


def test_a_view_created_with_ansi_nulls_off_is_kept_whole_with_its_reason(corpus_snapshot):
    artefact, reader = lower_text(corpus_snapshot, "CREATE VIEW sales.vTest AS SELECT o.OrderId FROM sales.[Order] AS o",
                                  ansi_nulls=False)

    assert checked(artefact, reader) == []
    assert query(artefact).kind == "opaque_fragment" and "ANSI_NULLS OFF" in query(artefact).attrs["reason"]
    assert artefact.body.attrs["ansi_nulls"] is False


def test_references_outside_the_catalog_are_external_references_not_failures(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot, "SELECT o.name, c.CustomerId FROM sys.objects AS o, ref.Customer AS c")

    [external] = of_kind(artefact, "external_reference")
    assert external.attrs["name"] == "sys.objects" and "system object" in external.attrs["reason"]
    [fragment] = of_kind(artefact, "opaque_fragment")
    assert fragment.attrs["text"] == "o.name" and "external reference" in fragment.attrs["reason"]


def test_parameters_and_function_calls_with_catalog_defaults_keep_their_types(corpus_snapshot):
    artefact, reader = lower_text(
        corpus_snapshot,
        "CREATE FUNCTION sales.fnTest (@s varchar(20)) RETURNS int AS BEGIN RETURN LEN(@s + 'x') END",
        kind="scalar_function", parameters=[PARAM_TEXT], returns=RETURNS_INT)

    assert checked(artefact, reader) == []
    assert artefact.assumptions and "CONCAT_NULL_YIELDS_NULL" in artefact.assumptions[0]
    [concat] = of_kind(artefact, "concat_null_propagating")
    # A variable's collation is the database default (coercible-default), as a literal's is.
    assert concat.value_type.param("origin") == "default" and concat.value_type.param("length") == 21


# ---------------------------------------------------------------- positions, comments, hints, opaque

def test_every_node_has_a_source_position_and_comments_annotate_the_nearest_node(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot,
        "SELECT o.OrderId, -- the key\n       o.Status\nFROM sales.[Order] AS o")

    assert all(n.span is not None and n.span.length > 0 for n in artefact.nodes())
    [annotated] = [n for n in artefact.nodes() if n.comments]
    # A comment after a select item on its own line annotates that item.
    assert annotated.attrs["output_name"] == "OrderId" and "the key" in annotated.comments[0]

    own_line = lowered_and_checked(
        corpus_snapshot, "SELECT o.OrderId,\n       -- the status\n       o.Status\nFROM sales.[Order] AS o")
    [annotated] = [n for n in own_line.nodes() if n.comments]
    assert annotated.attrs["output_name"] == "Status"


def test_query_hints_are_kept_on_their_nodes_and_reported_as_not_applicable(corpus_snapshot):
    artefact, reader = lower_text(
        corpus_snapshot,
        "CREATE FUNCTION sales.fnTest (@n int) RETURNS TABLE AS RETURN (SELECT o.OrderId FROM sales.[Order] AS o "
        "WITH (NOLOCK) JOIN ref.Customer AS c ON c.CustomerId = o.CustomerId)",
        kind="inline_function", parameters=[PARAM_INT])
    assert checked(artefact, reader) == []
    assert [h.attrs["hints"] for h in of_kind(artefact, "read_table") if "hints" in h.attrs] == [["NOLOCK"]]
    assert [h["hint"] for h in artefact.hints] == ["NOLOCK"]

    # OPTION is not allowed in a view or a function: it lowers on a SELECT statement of a procedure or batch.
    from migrt.sources.sqlserver import extractor, parser
    from migrt.sources.sqlserver.lowering import Lowering
    text = "SELECT o.OrderId FROM sales.[Order] AS o OPTION (RECOMPILE, MAXDOP 1)"
    parsed = parser.parse(reader.snapshot.get("RetailSample").identity, text,
                          reader.value("RetailSample", "compatibility_level", extractor.SOURCE), True)
    lowering_ = Lowering(reader, "RetailSample.sales.vTest", parsed)
    [batch] = list(parsed.tree.Batches)
    plan, _ = lowering_.select_statement(batch.Statements[0])
    assert plan.attrs["query_hints"] == ["RECOMPILE", "MAXDOP 1"]
    for number, node in enumerate(plan.walk(), start=1):
        node.id = f"n{number}"
    assert [(h["hint"], h["node"]) for h in lowering_.hints(plan)] == [("RECOMPILE", "n1"), ("MAXDOP 1", "n1")]


def test_what_cannot_be_expressed_is_an_opaque_fragment_with_its_reason(corpus_snapshot):
    whole = lowered_and_checked(
        corpus_snapshot, "SELECT o.OrderId FROM sales.[Order] AS o FOR XML AUTO")
    [fragment] = of_kind(whole, "opaque_fragment")
    assert query(whole) is fragment and "FOR XML" in fragment.attrs["reason"]
    assert fragment.attrs["text"].endswith("FOR XML AUTO") and fragment.attrs["scope_barrier"] is False
    assert "RetailSample.sales.Order" in whole.dependencies

    one_item = lowered_and_checked(
        corpus_snapshot, "SELECT o.OrderId, Odd = FOOBAR(o.Status) FROM sales.[Order] AS o")
    [fragment] = of_kind(one_item, "opaque_fragment")
    assert fragment.role == "output" and fragment.attrs["output_name"] == "Odd"
    assert "FOOBAR" in fragment.attrs["reason"] and fragment.attrs["text"] == "FOOBAR(o.Status)"
    assert query(one_item).kind == "project"


def test_a_literal_inside_a_marked_secret_lowers_to_a_named_setting_never_the_literal(corpus_snapshot):
    artefact, reader = lower_text(
        corpus_snapshot,
        "CREATE VIEW sales.vTest AS SELECT 'Server=hq;User ID=loader;Password=Hunter2!;' AS Conn, "
        "'plain' AS Other FROM sales.[Order] AS o")

    conn, other = (output(artefact, n) for n in ("Conn", "Other"))
    assert conn.kind == "literal" and conn.attrs["secret_setting"].startswith("RetailSample.sales.vTest.secret_")
    assert "value" not in conn.attrs and other.attrs["value"] == "plain"
    assert "Hunter2" not in str({n.id: n.attrs for n in artefact.nodes()})
    assert conn.attrs["secret_setting"] in artefact.secret_settings


# ---------------------------------------------------------------- functions

PARAM_INT = {"name": "@n", "declared_type": "int", "system_type": "int", "max_length": 4, "precision": 10,
             "scale": 0, "direction": "input", "readonly": False, "nullable": True, "default": None}
PARAM_TEXT = {"name": "@s", "declared_type": "varchar", "system_type": "varchar", "max_length": 20, "precision": 0,
              "scale": 0, "direction": "input", "readonly": False, "nullable": True, "default": None}
RETURNS_INT = {"declared_type": "int", "system_type": "int", "max_length": 4, "precision": 10, "scale": 0}


def test_a_scalar_function_lowers_its_parameters_and_returned_expression(corpus_snapshot):
    artefact, reader = lower_text(
        corpus_snapshot,
        "CREATE FUNCTION sales.fnTest (@n int) RETURNS int AS BEGIN RETURN @n / 2 END",
        kind="scalar_function", parameters=[PARAM_INT], returns=RETURNS_INT)

    assert checked(artefact, reader) == []
    assert artefact.artefact_type == "scalar_function" and artefact.body.kind == "scalar_function_body"
    [parameter] = artefact.body.children_with("parameter")
    assert parameter.kind == "declare_parameter" and parameter.attrs["name"] == "@n"
    [ret] = [n for n in artefact.nodes() if n.kind == "return"]
    assert ret.child("argument").kind == "integer_divide"
    assert ret.child("argument").children_with("argument")[0].kind == "variable"


def test_procedural_statements_in_a_function_body_are_scope_barriers_for_the_next_ticket(corpus_snapshot):
    artefact, reader = lower_text(
        corpus_snapshot,
        "CREATE FUNCTION sales.fnTest (@n int) RETURNS int AS BEGIN DECLARE @x int = @n; SET @x += 1; RETURN @x END",
        kind="scalar_function", parameters=[PARAM_INT], returns=RETURNS_INT)

    assert checked(artefact, reader) == []
    barriers = [n for n in of_kind(artefact, "opaque_fragment") if n.role == "statement"]
    assert len(barriers) == 2 and all(b.attrs["scope_barrier"] for b in barriers)
    assert all("procedural" in b.attrs["reason"] and "#43" in b.attrs["reason"] for b in barriers)
    [ret] = of_kind(artefact, "return")
    assert ret.child("argument").kind == "opaque_fragment"      # @x was declared by a barrier


def test_dynamic_sql_has_its_own_node_kind_with_its_text_expression_lowered(corpus_snapshot):
    artefact, reader = lower_text(
        corpus_snapshot,
        "CREATE FUNCTION sales.fnTest (@s varchar(20)) RETURNS int AS BEGIN EXEC('SELECT ' + @s); RETURN 1 END",
        kind="scalar_function", parameters=[PARAM_TEXT], returns=RETURNS_INT)

    assert checked(artefact, reader) == []
    [dynamic] = of_kind(artefact, "dynamic_sql_execution")
    assert dynamic.attrs["scope_barrier"] is True and dynamic.attrs["manual"] is True
    assert dynamic.child("text").kind == "concat_null_propagating"
    assert [c.kind for c in dynamic.child("text").children_with("argument")] == ["literal", "variable"]
    assert not of_kind(artefact, "opaque_fragment")


def test_an_inline_table_function_is_one_query_and_a_multi_statement_one_declares_its_rows(corpus_snapshot):
    inline, reader = lower_text(
        corpus_snapshot,
        "CREATE FUNCTION sales.fnTest (@n int) RETURNS TABLE AS RETURN (SELECT o.OrderId FROM sales.[Order] AS o WHERE o.OrderId = @n)",
        kind="inline_function", parameters=[PARAM_INT])
    assert checked(inline, reader) == []
    assert inline.body.kind == "table_function_body" and inline.body.attrs["form"] == "inline"
    assert chain(inline.body.child("query")) == ["project", "filter", "read_table"]

    multi, reader = lower_text(
        corpus_snapshot,
        "CREATE FUNCTION sales.fnTest (@n int) RETURNS @t TABLE (A int NOT NULL, B varchar(5) NULL) AS BEGIN "
        "INSERT INTO @t VALUES (@n, 'x'); RETURN END",
        kind="table_function", parameters=[PARAM_INT])
    assert checked(multi, reader) == []
    assert multi.body.attrs["form"] == "multi_statement"
    assert [c["name"] for c in multi.body.attrs["returns"]] == ["A", "B"]
    assert [n.kind for n in multi.body.children_with("statement")] == ["opaque_fragment", "return"]


# ---------------------------------------------------------------- the checker

def test_the_checker_refuses_string_comparisons_without_a_rule_and_operands_that_disagree(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot, "SELECT c.CustomerId FROM ref.Customer AS c WHERE c.Region = 'emea'")
    [compare] = of_kind(artefact, "compare")
    compare.attrs["rule"] = None
    findings = checker.check(artefact, lambda table: {"CustomerId", "Region"})
    assert any("rule" in f.message for f in findings)

    compare.attrs["rule"] = {"collation": "x"}
    compare.children[1].type = type(compare.children[1].value_type).of("integer", bits=32, signed=True)
    findings = checker.check(artefact, lambda table: {"CustomerId", "Region"})
    assert any("operand types" in f.message for f in findings)


# ---------------------------------------------------------------- the corpus

def corpus_modules(snapshot):
    return [o for o in snapshot.objects if o.kind in ("view", "scalar_function", "inline_function", "table_function")
            and o.has_fact("source_text") and o.fact("extractable").value]


def test_the_checker_passes_on_every_lowered_view_and_function_in_the_corpus(corpus_snapshot):
    from migrt.core.catalog import CatalogReader
    from migrt.sources.sqlserver import extractor, names, parser

    reader = CatalogReader(corpus_snapshot, names.resolve)
    modules = corpus_modules(corpus_snapshot)
    assert len(modules) == 45
    failures = {}
    for module in modules:
        database = module.identity.split(".")[0]
        parsed = parser.parse(module.identity, reader.value(module.identity, "source_text"),
                              reader.value(database, "compatibility_level", extractor.SOURCE),
                              reader.value(module.identity, "quoted_identifier", extractor.SOURCE))
        artefact = lowering.lower_artefact(reader, module.identity, parsed)
        findings = checker.check(artefact, lowering.columns_of(reader))
        if findings:
            failures[module.identity] = [f"{f.node_id}: {f.message}" for f in findings]
    assert failures == {}


def test_lowering_is_deterministic(corpus_snapshot):
    sql = "CREATE VIEW sales.vTest AS SELECT o.OrderId, COUNT(*) AS N FROM sales.[Order] AS o GROUP BY o.OrderId"
    first, _ = lower_text(corpus_snapshot, sql)
    second, _ = lower_text(corpus_snapshot, sql)
    assert first.document() == second.document()


# ---------------------------------------------------------------- review follow-ups

def test_an_explicit_collate_beats_the_columns_collation_in_the_rule(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot,
        "SELECT c.CustomerId FROM ref.Customer AS c WHERE c.Name COLLATE Latin1_General_CS_AS = N'x'")

    [compare] = of_kind(artefact, "compare")
    assert compare.attrs["rule"]["origin"] == "explicit" and compare.attrs["rule"]["case_sensitive"] is True
    assert compare.attrs["rule"]["collation"] == "Latin1_General_CS_AS"
    assert compare.children_with("argument")[0].attrs["collate"] == "Latin1_General_CS_AS"


def test_string_keys_that_sort_group_partition_or_deduplicate_name_their_rule(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot,
        "SELECT DISTINCT c.Region, RANK() OVER (PARTITION BY c.Name ORDER BY c.Email) AS R "
        "FROM ref.Customer AS c ORDER BY c.Region OFFSET 0 ROWS")
    grouped = lowered_and_checked(corpus_snapshot, "SELECT p.Sku FROM ref.Product AS p GROUP BY p.Sku")

    [window] = of_kind(artefact, "window")
    assert window.child("partition").attrs["rule"]["case_sensitive"] is False
    assert window.child("order").attrs["rule"]["collation"] == "SQL_Latin1_General_CP1_CI_AS"
    [sort] = of_kind(artefact, "sort")
    assert sort.child("order").attrs["rule"]["origin"] == "column"
    [distinct] = of_kind(artefact, "distinct")
    assert distinct.attrs["rules"][0]["collation"] == "SQL_Latin1_General_CP1_CI_AS"
    [key] = of_kind(grouped, "aggregate")[0].children_with("key")
    assert key.attrs["rule"]["case_sensitive"] is True          # Sku is case sensitive


def test_numeric_and_binary_literals_in_a_marked_secret_are_named_settings_too(corpus_snapshot):
    from migrt.core.secrets import SecretSpan
    from migrt.sources.sqlserver import extractor

    sql = "CREATE VIEW sales.vTest AS SELECT 7 AS B FROM sales.[Order] AS o"
    start = sql.index("7")
    marked = extractor._marked("source_text", [SecretSpan(start, 1, "test pattern")])
    from migrt.core.catalog import CatalogObject, CatalogReader, Fact, Snapshot
    from migrt.sources.sqlserver import names, parser
    from lowering_helpers import P
    facts = [Fact("skeleton", "source_text", sql, P), Fact("skeleton", "extractable", True, P),
             Fact("skeleton", "secrets", marked, P), Fact(extractor.SOURCE, "ansi_nulls", True, P),
             Fact(extractor.SOURCE, "quoted_identifier", True, P)]
    module = CatalogObject("RetailSample.sales.vTest", "view", tuple(facts))
    reader = CatalogReader(Snapshot.of("test", list(corpus_snapshot.objects) + [module]), names.resolve)
    parsed = parser.parse("RetailSample.sales.vTest", sql,
                          reader.value("RetailSample", "compatibility_level", extractor.SOURCE), True)
    lowered = lowering.lower_artefact(reader, "RetailSample.sales.vTest", parsed)

    [literal] = of_kind(lowered, "literal")
    assert "value" not in literal.attrs and literal.attrs["secret_setting"].endswith(".secret_1")


def test_typing_follows_sql_servers_for_money_totals_ceiling_nullif_and_untyped_null(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot,
        "SELECT A = CEILING(p.WeightKg), B = NULLIF(o.OrderId, 0.5), C = NULL, "
        "D = CASE WHEN o.Status = 1 THEN p.WeightKg ELSE o.OrderId END, E = DATALENGTH(CAST(N'x' AS nvarchar(max))) "
        "FROM sales.[Order] AS o, ref.Product AS p")

    assert (output(artefact, "A").value_type.name, output(artefact, "A").value_type.int_param("scale")) == ("decimal", 0)
    assert output(artefact, "B").value_type.name == "integer"            # NULLIF takes its first argument's type
    assert output(artefact, "C").value_type.name == "integer"            # an untyped NULL is an int
    d = output(artefact, "D").value_type
    assert (d.int_param("precision"), d.int_param("scale")) == (13, 3)   # decimal(9,3) with decimal(10,0)
    assert output(artefact, "E").value_type.int_param("bits") == 64


def test_the_checker_finds_a_column_a_derived_relation_does_not_offer(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot, "SELECT d.OrderId FROM (SELECT o.OrderId FROM sales.[Order] AS o) AS d ORDER BY d.OrderId OFFSET 0 ROWS")
    outer = [o for o in of_kind(artefact, "column") if o.attrs["relation"] == "d"]
    outer[0].attrs["column"] = "Missing"

    assert any("has no column Missing" in f.message for f in checker.check(artefact, lambda t: {"OrderId"}))


def test_hints_inside_an_opaque_fragment_are_still_reported(corpus_snapshot):
    artefact = lowered_and_checked(
        corpus_snapshot, "SELECT o.OrderId FROM sales.[Order] AS o WITH (NOLOCK) FOR XML AUTO")

    assert [h["hint"] for h in artefact.hints] == ["NOLOCK"]
