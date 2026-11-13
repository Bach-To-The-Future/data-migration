"""The report data (spec section 15), first cut: the one deterministic file both report
documents will be rendered from. Run-specific fields (run id, times, durations) sit in
their own section, so a re-run's data is identical outside it.

In a preview run every artefact's verdict is "Not reconciled", its validation "Not run",
and it sits under the one sign-off heading "Not reconciled: preview run"; nothing in it
is converted (ADR 0012).
"""
from __future__ import annotations

import json

# 0.3: the extraction section; secrets and name adjustments per artefact. 0.4: recognised
# artefacts as manual work; the extraction section's completeness summary, assumed
# collations, skipped cross-checks and folders. 0.5: the hints of each artefact, reported as not applicable on
# the target. 0.6: the delivered project's files, and every kind of artefact (issue #44). 0.7: a construct that is
# manual work only for want of something not built yet carries `waits_for`, the tickets it waits for.
SCHEMA_VERSION = "report-data/0.7"
PREVIEW_BANNER = ("Preview run: reconciliation was off. Nothing in this report was checked against the "
                  "original, and nothing in it is converted.")
NOT_RECONCILED = "Not reconciled"
# What the report says of every query hint: the target has nothing to apply it to (spec section 4, item 3).
HINT_NOT_APPLICABLE = "not applicable on the target"
PREVIEW_GROUP = "Not reconciled: preview run"

# Construct statuses (spec section 15). In a preview run a generated construct is
# "Not reconciled (preview run)"; one the target cannot generate is manual work.
CONSTRUCT_NOT_RECONCILED = "Not reconciled (preview run)"
CONSTRUCT_MANUAL = "manual work"

# A recognised artefact type (spec section 15): never converted, so never reconciled or
# validated, and always manual work, in a preview run as in any other.
RECOGNISED_STATUS = "Recognised, not converted"
MANUAL_GROUP = "Manual work"
RECOGNISED_REASON = "A recognised artefact type: the tool inventories it and lists it as manual work, with no target code."


def recognised_artefact(*, name: str, type: str, inventory: dict, references: list[dict],
                        references_unavailable: list[dict], entry_points: list[str], secrets: list[str],
                        assumed_values: list[dict]) -> dict:
    return {
        "name": name,
        "type": type,
        "verdict": NOT_RECONCILED,
        "validation": "Not run",
        "status": RECOGNISED_STATUS,
        "sign_off_group": MANUAL_GROUP,
        "reason": RECOGNISED_REASON,
        "inventory": inventory,
        "references": references,
        "references_unavailable": references_unavailable,
        "entry_points": entry_points,
        "secrets": [{"setting": s, "text": "A likely secret in the artefact, shown as this named setting."}
                    for s in secrets],
        "assumed_values": assumed_values,
    }


def preview_artefact(*, name: str, artefact_type: str, area: str, output: str, layer1: dict,
                     constructs: list[dict], degradations: list[dict], secrets: list[dict],
                     name_adjustments: dict[str, dict[str, str]], hints: list[dict] | None = None,
                     flags: list[dict] | None = None) -> dict:
    return {
        "name": name,
        "type": artefact_type,
        "area": area,
        "verdict": NOT_RECONCILED,
        "validation": "Not run",
        "status": NOT_RECONCILED,
        "sign_off_group": PREVIEW_GROUP,
        "flags": flags or [],
        "output": output,
        "layer1": layer1,
        "constructs": constructs,
        "hints": hints or [],
        "degradations": degradations,
        "secrets": secrets,
        "name_adjustments": name_adjustments,
    }


def preview_report(*, engagement: str, snapshot: str, source: dict, target: dict, versions: dict,
                   artefacts: list[dict], recognised: list[dict], extraction: dict, project: dict,
                   degradations: list[dict], run_specific: dict) -> dict:
    groups = {PREVIEW_GROUP: [a["name"] for a in artefacts]}
    if recognised:
        groups[MANUAL_GROUP] = sorted(r["name"] for r in recognised)
    return {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "preview": True,
            "banner": PREVIEW_BANNER,
            "engagement": engagement,
            "snapshot": snapshot,
            "source": source,
            "target": target,
            "versions": versions,
        },
        "sign_off": {"groups": groups, "signature_block": False},
        "artefacts": sorted(artefacts, key=lambda a: a["name"]),
        "recognised_artefacts": sorted(recognised, key=lambda a: a["name"]),
        "extraction": extraction,
        "project": project,
        "degradations": degradations,
        "run_specific": run_specific,
    }


def dumps(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
