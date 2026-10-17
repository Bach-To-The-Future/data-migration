# Report prototype (throwaway)

PROTOTYPE for [What does the conversion report look like?](https://github.com/Bach-To-The-Future/data-migration/issues/9). Invented data, not a real run.

- `report-data.json`: the one deterministic source every page is rendered from.
- `render.py`: stdlib-only renderer. `python prototype/report/render.py` writes `out/`.
- `pdf.sh`: renders every variant to PDF with WeasyPrint (BSD) twice in a throwaway container and checks the two renders are byte-identical.
- `out/index.html`: open it in a browser. The floating bar switches document (sign-off or engineering) and variant (`?doc=signoff&variant=A`), with arrow keys too, and links each variant's PDF.

Variants:

| Document | Variant | Idea |
|---|---|---|
| Client sign-off | A | One-page verdict letter: headline, bar, plain lists by what the reader must do, one signature block |
| Client sign-off | B | By business area: a summary table, then a section and a signature block per area |
| Client sign-off | C | Questions and answers |
| Engineering | A | Artefact by artefact, then registers |
| Engineering | B | Register of everything not clean, worst first, then every artefact as an appendix |
