"""The report data (spec section 15), first cut: the one deterministic file both report
documents will be rendered from. Run-specific fields (run id, times, durations) sit in
their own section, so a re-run's data is identical outside it.

In a preview run every artefact's verdict is "Not reconciled", its validation "Not run",
and it sits under the one sign-off heading "Not reconciled: preview run"; nothing in it
is converted (ADR 0012).
"""
from __future__ import annotations

import json

SCHEMA_VERSION = "report-data/0.1"
PREVIEW_BANNER = ("Preview run: reconciliation was off. Nothing in this report was checked against the "
                  "original, and nothing in it is converted.")
NOT_RECONCILED = "Not reconciled"
PREVIEW_GROUP = "Not reconciled: preview run"

# Construct statuses (spec section 15). In a preview run a generated construct is not
# reconciled; one the target cannot generate is manual work.
CONSTRUCT_NOT_RECONCILED = "not reconciled"
CONSTRUCT_MANUAL = "manual work"


def preview_artefact(*, name: str, artefact_type: str, area: str, output: str, layer1: dict,
                     constructs: list[dict], degradations: list[dict]) -> dict:
    return {
        "name": name,
        "type": artefact_type,
        "area": area,
        "verdict": NOT_RECONCILED,
        "validation": "Not run",
        "status": NOT_RECONCILED,
        "sign_off_group": PREVIEW_GROUP,
        "flags": [],
        "output": output,
        "layer1": layer1,
        "constructs": constructs,
        "degradations": degradations,
    }


def preview_report(*, engagement: str, snapshot: str, source: dict, target: dict, versions: dict,
                   artefacts: list[dict], degradations: list[dict], run_specific: dict) -> dict:
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
        "sign_off": {"groups": {PREVIEW_GROUP: [a["name"] for a in artefacts]}, "signature_block": False},
        "artefacts": sorted(artefacts, key=lambda a: a["name"]),
        "degradations": degradations,
        "run_specific": run_specific,
    }


def dumps(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
