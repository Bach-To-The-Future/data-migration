---
status: accepted
---

# A reviewer may supply target code for one region, as a tested client lesson

Converted files are never hand-edited and are always regenerated. Some artefacts will still get stuck: the rules get them wrong, repair runs out of attempts, and no lesson the tool can apply during a run fixes them. We decided that a reviewer may then supply target code for one region of one artefact, as a lesson of the kind called region replacement. It is a client lesson whose trigger matches that artefact only. Its code is put into the output each time the artefact is regenerated, it goes through every check like any other code, and the report flags it as reviewer-supplied, naming the reviewer. We chose this because without it, a delivery team with a stuck artefact would edit the output by hand, where nothing would test it, record it or survive the next run.

The full decision is in [the resolution of the ticket that made it](https://github.com/Bach-To-The-Future/data-migration/issues/23#issuecomment-6211827394).

## Considered options

- **No reviewer-supplied code; a stuck region stays manual work.** It keeps the rule that every line comes from the tool. Rejected: manual work is then done outside the tool, untested and lost when the artefact is regenerated.

## Consequences

- **"Never hand-edited" still holds for files.** A region replacement is input to the tool, not an edit of its output.
- **It never generalises and never goes global.** It belongs to one artefact of one client, and is deleted with that client's data.
- **It is not a correctness bypass.** It passes the same confirmation gate and the same verdict. The type mapping, the handling of silent differences, prompts and tests remain the tool's own, and no lesson of any kind can change them.
- **Readers can see how much of the output a person wrote.** The report counts and lists every region replacement.
