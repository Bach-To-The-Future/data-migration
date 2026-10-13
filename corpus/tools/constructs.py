"""Detect which T-SQL constructs an artefact uses, by pattern.

This is a manifest aid, not a parser: it strips comments and string literals,
then looks for keywords. It can miss a construct or report one that is not
really there; treat the result as an index for choosing artefacts.
"""
import re

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_STRING = re.compile(r"N?'(?:[^']|'')*'")

_I = re.IGNORECASE | re.DOTALL

# Checked against the code with comments removed and string literals blanked.
_CODE_PATTERNS = {
    "cursor": r"\bCURSOR\b.*?\bFOR\b",
    "temp_table": r"(?<![\w@])#{1,2}\w+",
    "table_variable": r"\bDECLARE\s+@\w+\s+(?:AS\s+)?TABLE\b|\bRETURNS\s+@\w+\s+TABLE\b",
    "table_valued_parameter": r"\bREADONLY\b",
    "transaction": r"\b(?:BEGIN|COMMIT|ROLLBACK)\s+TRAN(?:SACTION)?\b",
    "savepoint": r"\bSAVE\s+TRAN(?:SACTION)?\b",
    "xact_abort": r"\bSET\s+XACT_ABORT\b",
    "try_catch": r"\bBEGIN\s+TRY\b",
    "throw": r"\bTHROW\b",
    "raiserror": r"\bRAISERROR\b",
    "at_at_error": r"@@ERROR\b",
    "goto": r"\bGOTO\b",
    "while_loop": r"\bWHILE\b",
    "if_else": r"\bIF\b",
    "return_code": r"\bRETURN\s+\(?\s*(?:-?\d+|@\w+)\s*\)?\s*;",
    "output_parameter": r"@\w+\s+[\w\(\), ]+?\bOUT(?:PUT)?\b",
    "merge": r"\bMERGE\b(?!\s+JOIN)",
    "output_clause": r"\bOUTPUT\s+(?:inserted|deleted|\$action)\b",
    "rowcount": r"@@ROWCOUNT\b",
    "identity_function": r"\bSCOPE_IDENTITY\s*\(|@@IDENTITY\b|\bIDENT_CURRENT\s*\(",
    "update_from": r"\bUPDATE\s+[\[\]\w.]+\s+SET\b[^;]*?\bFROM\b",
    "delete_with_join": r"\bDELETE\s+(?:FROM\s+)?[\[\]\w.]+\s+(?:OUTPUT\b[^;]*?)?FROM\b",
    "top_with_ties": r"\bWITH\s+TIES\b",
    "window_function": r"\bOVER\s*\(",
    "cte": r"\bWITH\s+[\[\]\w]+\s*(?:\([^)]*\)\s*)?AS\s*\(",
    "pivot": r"\b(?:UN)?PIVOT\s*\(",
    "apply": r"\b(?:CROSS|OUTER)\s+APPLY\b",
    "rollup_cube": r"\bROLLUP\b|\bCUBE\b|\bGROUPING\s+SETS\b",
    "xml": r"\bFOR\s+XML\b|\bOPENXML\b|\.nodes\s*\(|\.value\s*\(|\.query\s*\(",
    "json": r"\bOPENJSON\b|\bFOR\s+JSON\b|\bJSON_VALUE\b|\bJSON_QUERY\b",
    "collate": r"\bCOLLATE\b",
    "datediff": r"\bDATEDIFF(?:_BIG)?\s*\(",
    "datepart": r"\bDATEPART\s*\(|\bDATENAME\s*\(",
    "convert_style": r"\bCONVERT\s*\([^()]*(?:\([^()]*\)[^()]*)*,[^()]*,\s*\d+\s*\)",
    "print": r"\bPRINT\b",
    "instead_of_trigger": r"\bINSTEAD\s+OF\b",
    "inserted_deleted": r"\b(?:FROM|JOIN)\s+(?:inserted|deleted)\b",
    "update_column_test": r"\bIF\s+(?:NOT\s+)?UPDATE\s*\(",
    "native_compilation": r"\bNATIVE_COMPILATION\b",
    "temporal": r"\bFOR\s+SYSTEM_TIME\b",
    "sequence": r"\bNEXT\s+VALUE\s+FOR\b",
    "remote_query": r"\bOPENQUERY\b|\bOPENROWSET\b|\bOPENDATASOURCE\b",
    "execute_as": r"\bEXECUTE\s+AS\b|\bEXEC\s+AS\b",
    "system_catalog": r"\bsys\.(?!sp_)\w+|\bINFORMATION_SCHEMA\.\w+",
}

# Dynamic SQL and procedure calls need care: EXEC (@sql) and EXEC proc look alike.
_DYNAMIC_SQL = re.compile(r"\bsp_executesql\b|\bEXEC(?:UTE)?\s*\(\s*(?:@|N?')", _I)
_EXEC_PROC = re.compile(r"\bEXEC(?:UTE)?\s+(?:@\w+\s*=\s*)?(?!sp_executesql\b|sys\.sp_executesql\b|AS\b)[\[\w]", _I)

_COMPILED = {name: re.compile(pattern, _I) for name, pattern in _CODE_PATTERNS.items()}


def strip_comments(sql):
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub(" ", sql))


def detect(sql):
    """Return the sorted list of constructs found in one artefact's source."""
    no_comments = strip_comments(sql)
    code = _STRING.sub("''", no_comments)
    found = {name for name, rx in _COMPILED.items() if rx.search(code)}
    # Keep string literals for dynamic SQL: EXEC ('...') starts with a quote.
    if _DYNAMIC_SQL.search(no_comments):
        found.add("dynamic_sql")
    if _EXEC_PROC.search(code):
        found.add("procedure_call")
    return sorted(found)


def code_lines(sql):
    """Lines that are not blank after comments are removed."""
    return sum(1 for line in strip_comments(sql).splitlines() if line.strip())
