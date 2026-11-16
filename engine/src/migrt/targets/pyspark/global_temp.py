"""How the PySpark target treats a global temp table (`##t`, visible to every session on the source).

This is the target half's wording (ADR 0019): the source half says only that SQL Server makes it visible to every
session, and the report puts the two side by side. The target has no session shared by other clients to give a table to,
so it plans a scratch table scoped to the run, and the caveat says what that does not reproduce. Nothing is generated
yet: the statements that make and use one are manual work until procedural generation (issue #50) is built.
"""

TREATMENT = "planned: a scratch table scoped to the run, once procedural generation is built"
CAVEAT = ("A global temp table is generated as a scratch table scoped to the run, so cross-session visibility is not "
          "reproduced: no other session can see it.")
