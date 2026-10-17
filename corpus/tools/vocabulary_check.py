"""Check the draft operation vocabulary against the corpus, and render it as Markdown.

    python corpus/tools/vocabulary_check.py

Runs on the host with the standard library only. It reads
docs/spec-drafts/operation-vocabulary.json, scans every artefact in the sample
corpus and in corpus/public/export (run corpus/build.py first to create that),
and fails if the corpus uses a built-in function, an @@ value or a type that
no entry in the vocabulary accounts for.

Everything here is pattern matching on source text, not parsing. The counts
are an index of where to look, not a measurement.
"""
import collections
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = ROOT / "docs" / "spec-drafts" / "operation-vocabulary.json"
TARGET = ROOT / "docs" / "spec-drafts" / "operation-vocabulary.md"

BLOCK = re.compile(r"/\*.*?\*/", re.S)
LINE = re.compile(r"--[^\n]*")
STRING = re.compile(r"N?'(?:[^']|'')*'")
CALL = re.compile(r"(?<![\w.\]@#])([A-Za-z_][\w]*)\s*\(")
GLOBAL = re.compile(r"@@\w+")
TYPE_WORDS = """bit tinyint smallint int bigint decimal numeric money smallmoney float real date datetime datetime2
smalldatetime datetimeoffset time char nchar varchar nvarchar text ntext binary varbinary image uniqueidentifier xml
sql_variant hierarchyid geography geometry rowversion sysname""".split()


def load_corpus():
    files = sorted((ROOT / "corpus" / "sample" / "artefacts").rglob("*.sql"))
    export = ROOT / "corpus" / "public" / "export"
    public = sorted(export.rglob("*.sql")) if export.exists() else []
    code = {}
    for f in files + public:
        raw = f.read_text(encoding="utf-8", errors="replace")
        code[f] = STRING.sub("''", LINE.sub("", BLOCK.sub(" ", raw)))
    return code, len(files), len(public)


# Expression operations are spelled as function calls, so MAX means MAX(...) and not varchar(max),
# and LEFT means LEFT(...) and not LEFT JOIN. These few are spelled as bare words.
BARE_WORD_EXPRESSIONS = {"CASE", "AND", "OR", "BETWEEN", "IN", "LIKE", "CURRENT_TIMESTAMP"}


def all_entries(vocab):
    for section in ("types", "node_kinds", "annotations"):
        for e in vocab[section]:
            yield section, e
    for group in vocab["groups"]:
        for e in group["operations"]:
            e["_part"] = group["part"]
            yield group["group"], e


def spelling_pattern(spelling):
    s = spelling.upper()
    if s.startswith("@@"):
        return re.escape(s) + r"\b"
    if s.endswith("("):
        return r"(?<![\w.])" + re.escape(s[:-1]) + r"\s*\("
    return r"(?<![\w.@#])" + r"\s+".join(re.escape(p) for p in s.split()) + r"(?![\w])"


def detector(entry):
    if "detect" in entry:
        return re.compile(entry["detect"], re.I | re.S) if entry["detect"] else None
    if not entry["tsql"]:
        return None
    as_calls = entry.get("_part") == "expression"
    patterns = []
    for s in entry["tsql"]:
        if as_calls and not s.endswith("(") and s.upper() not in BARE_WORD_EXPRESSIONS and not s.startswith("@@"):
            s = s + "("
        patterns.append(spelling_pattern(s))
    return re.compile("|".join(patterns), re.I)


def main():
    vocab = json.loads(SOURCE.read_text(encoding="utf-8"))
    entries = list(all_entries(vocab))
    names = [e["name"] for _, e in entries]
    duplicates = [n for n, c in collections.Counter(names).items() if c > 1]
    if duplicates:
        sys.exit(f"duplicate entries: {duplicates}")

    code, sample_count, public_count = load_corpus()

    # Which artefacts use each entry.
    used_by = {}
    for _, e in entries:
        rx = detector(e)
        used_by[e["name"]] = None if rx is None else sum(1 for text in code.values() if rx.search(text))

    # Coverage: every built-in called, every @@ value and every type in the corpus must be accounted for.
    spellings = set()
    for _, e in entries:
        for s in e["tsql"]:
            spellings.add(s.upper().rstrip("("))
    accounted = spellings | {w.upper() for w in vocab["syntax_only"]["words"]} | {w.upper() for w in vocab["aliases_seen"]["words"]}
    calls, globals_, types = collections.Counter(), collections.Counter(), collections.Counter()
    for text in code.values():
        for name in {m.group(1).upper() for m in CALL.finditer(text)}:
            calls[name] += 1
        for name in {g.upper() for g in GLOBAL.findall(text)}:
            globals_[name] += 1
        for t in TYPE_WORDS:
            if re.search(r"(?<![\w.\[@#])\[?" + t + r"\]?(?![\w.])", text, re.I):
                types[t.upper()] += 1
    unplaced = sorted(
        [f"{n}( in {c}" for n, c in calls.items() if n not in accounted]
        + [f"{n} in {c}" for n, c in globals_.items() if n not in accounted]
        + [f"type {n} in {c}" for n, c in types.items() if n not in accounted]
    )

    operations = [e for g in vocab["groups"] for e in g["operations"]]
    exercised = sum(1 for e in operations if e["x"])
    render(vocab, entries, operations, used_by, exercised, sample_count, public_count, unplaced)

    print(f"{len(vocab['types'])} types, {len(vocab['node_kinds'])} node kinds, {len(vocab['annotations'])} annotations, "
          f"{len(operations)} operations ({exercised} exercised by the hand conversion)")
    print(f"corpus: {sample_count} sample and {public_count} public artefacts scanned; "
          f"{len(calls)} distinct call names, {len(globals_)} @@ values, {len(types)} types")
    if public_count == 0:
        print("WARNING: corpus/public/export is missing, so only the sample corpus was checked. Run corpus/build.py.")
    if unplaced:
        print("NOT PLACED:", "; ".join(unplaced))
        sys.exit(1)
    print("every call name, @@ value and type in the corpus is accounted for")


def render(vocab, entries, operations, used_by, exercised, sample_count, public_count, unplaced):
    def cell(text):
        return str(text).replace("|", "\\|")

    def used(name):
        n = used_by[name]
        return "" if n is None else str(n)

    def spell(e):
        return ", ".join(f"`{s}`" for s in e["tsql"])

    by_name = {e["name"]: e for _, e in entries}
    out = [
        "# Operation vocabulary, first draft",
        "",
        "Generated from `operation-vocabulary.json` by `python corpus/tools/vocabulary_check.py`. Edit the JSON, not this file.",
        "",
        "This is the first list of what the intermediate representation can say: its types, its operations and the node "
        "kinds that are not operations. It is a draft for the spec. The full vocabulary grows during the build.",
        "",
        "## Rules it follows",
        "",
    ]
    out += [f"{i}. {rule}" for i, rule in enumerate(vocab["rules"], 1)]
    out += [
        "",
        "## Size",
        "",
        f"- {len(vocab['types'])} types, {len(vocab['node_kinds'])} node kinds, {len(vocab['annotations'])} annotations.",
        f"- {len(operations)} operations, of which the hand conversion in `prototype/hand-conversion/` exercised {exercised}. "
        f"The other {len(operations) - exercised} are on paper only.",
        "",
        "## How to read the tables",
        "",
        "- **Comes from** lists T-SQL words that lower to the entry. An empty cell means the entry is chosen by meaning, "
        "not by spelling: `integer_divide` comes from `/` only when both operands are integers.",
        f"- **Artefacts** counts how many of the {sample_count + public_count} corpus artefacts written in SQL ({sample_count} sample, "
        f"{public_count} public) appear to use the entry. It is pattern matching on source text, not parsing, so treat it "
        "as an index of where to look. An empty cell means it cannot be counted that way.",
        "- **Hand** is ✓ where the hand conversion exercised the entry.",
        "",
        "## Types",
        "",
        "A type carries the behaviour that matters, not the source's name for it. The source's own type name is kept alongside for the report.",
        "",
        "| Type | Meaning | Comes from | Artefacts | Hand |",
        "|---|---|---|---|---|",
    ]
    out += [f"| `{cell(e['name'])}` | {cell(e['meaning'])} | {spell(e)} | {used(e['name'])} | {'✓' if e['x'] else ''} |" for e in vocab["types"]]
    out += [
        "",
        "## Node kinds that are not operations",
        "",
        "| Node kind | Meaning | Comes from | Artefacts | Hand |",
        "|---|---|---|---|---|",
    ]
    out += [f"| `{cell(e['name'])}` | {cell(e['meaning'])} | {spell(e)} | {used(e['name'])} | {'✓' if e['x'] else ''} |" for e in vocab["node_kinds"]]
    out += [
        "",
        "## Annotations",
        "",
        "Carried on nodes. None is an operation and none changes what a node means.",
        "",
        "| Annotation | Meaning | Comes from | Artefacts | Hand |",
        "|---|---|---|---|---|",
    ]
    out += [f"| `{cell(e['name'])}` | {cell(e['meaning'])} | {spell(e)} | {used(e['name'])} | {'✓' if e['x'] else ''} |" for e in vocab["annotations"]]

    parts = [("procedural", "Procedural operations"), ("query", "Query operations"), ("expression", "Expression operations")]
    for part, heading in parts:
        out += ["", f"## {heading}"]
        if part == "query":
            out += ["", "The query part is a logical plan: each query is a sequence of these steps, keeping the source's names and order."]
        for group in vocab["groups"]:
            if group["part"] != part:
                continue
            out += ["", f"### {group['group']}", "", "| Operation | Meaning | Comes from | Artefacts | Hand |", "|---|---|---|---|---|"]
            out += [f"| `{cell(e['name'])}` | {cell(e['meaning'])} | {spell(e)} | {used(e['name'])} | {'✓' if e['x'] else ''} |"
                    for e in group["operations"]]

    not_converted = ["dynamic_sql_execution", "schema_change", "permission_change", "external_reference"]
    out += [
        "",
        "## What in the corpus is deliberately not converted",
        "",
        "These have a place in the representation and no conversion. Each is recorded and reported as manual work.",
        "",
        "| Node kind or type | Artefacts | Why |",
        "|---|---|---|",
    ]
    reasons = {
        "dynamic_sql_execution": "A recognised construct: the text is built at run time, so there is nothing to lower.",
        "schema_change": "Schema migration is out of scope.",
        "permission_change": "Security is outside what the tool converts.",
        "external_reference": "The object is outside the catalog.",
    }
    out += [f"| `{n}` | {used(n)} | {reasons[n]} |" for n in not_converted]
    out += [f"| `unsupported_type(source_name)` | {used('unsupported_type(source_name)')} | "
            "No neutral meaning yet for these source types. |"]

    paper = sorted(((used_by[e["name"]], e["name"]) for e in operations if not e["x"] and used_by[e["name"]]), reverse=True)
    out += [
        "",
        "## Operations the corpus uses that the hand conversion did not exercise",
        "",
        "These are on paper only, and the corpus needs them. The most used come first.",
        "",
        "| Operation | Artefacts |",
        "|---|---|",
    ]
    out += [f"| `{cell(name)}` | {count} |" for count, name in paper]

    out += [
        "",
        "## What the check found",
        "",
        "Every built-in function called, every `@@` value and every type that appears in the corpus is accounted for "
        "by an entry above." if not unplaced else "**Not placed:** " + "; ".join(unplaced),
        "",
        "What that does and does not show:",
        "",
        "- It shows no built-in function, `@@` value or type in the corpus lacks an entry.",
        "- It does not show the entries are right. Nothing lowers into this vocabulary yet, and no target generates from it.",
        "- It cannot see operations chosen by meaning. Whether a `/` is integer or decimal division depends on types, which pattern matching does not have.",
        "- Words that are syntax and not operations, and five table aliases from XML shredding that look like function calls, "
        "are listed in the JSON so the check knows they were considered.",
    ]
    TARGET.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
