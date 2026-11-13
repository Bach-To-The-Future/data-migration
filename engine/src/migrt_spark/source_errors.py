"""The source's half of the error mapping, applied (ADR 0019; spec section 6, "Errors").

The library holds the target half only: what a Spark or Delta failure is, as a neutral error class with details. The
source's half, which turns a class into the source's number, severity, state and message, and says how far the error
reaches, is data the delivered project carries (`error-mapping.json`) and session state reads at startup, like the settings
file. This module is the one interpreter of that data and knows nothing of any particular source: another source brings
another file of the same form.

A template is text with `{detail}` placeholders. `{detail:type}` writes a vocabulary type in the source's own name for it.
A number, severity or state may instead be `{"detail": name}`, taken from the details. An entry may hold `variants`, each
with a `when` giving the values of the details it applies to, or `when_present` and `when_absent` naming details; a class with variants and no variant that fits is not covered, and the error
takes the source's reserved number rather than another error's."""
from __future__ import annotations

import dataclasses
import json
import pathlib
import re
from typing import Any

from .errors import MissingDetail

FORMAT = "migrt-error-mapping/1"
ENDS = ("statement", "scope", "batch")
_PLACEHOLDER = re.compile(r"\{(\w+)(?::(\w+))?\}")


@dataclasses.dataclass(frozen=True)
class Rendered:
    """An error as the source reports it."""
    number: int
    severity: int
    state: int
    message: str
    ends: str                          # statement, scope or batch
    catchable_at_same_level: bool
    exact_message: bool                # False: the failure did not carry what the message needs; the text is a stand-in
    unmapped: bool                     # True: the source has no entry for it, so it carries the reserved number
    reason: str | None = None          # why it is unmapped


class SourceErrors:
    def __init__(self, data: dict):
        if data.get("format") != FORMAT:
            raise ValueError(f"the error mapping has format {data.get('format')!r}; this library reads {FORMAT!r}")
        self._data = data
        self.classes: dict[str, dict] = data["classes"]
        self.reserved: dict = data["reserved"]
        self.type_names: dict[str, str] = data.get("type_names", {})

    @classmethod
    def load(cls, source: "str | pathlib.Path | dict | SourceErrors") -> "SourceErrors":
        if isinstance(source, SourceErrors):
            return source
        if isinstance(source, dict):
            return cls(source)
        return cls(json.loads(pathlib.Path(source).read_text(encoding="utf-8")))

    # ------------------------------------------------------------------ rendering

    def render(self, error_class: str, details: dict[str, Any] | None = None) -> Rendered:
        """The source's error for a class and its details. Never raises for a class the file lacks or a variant that does
        not fit: that is an unmapped error, flagged by its reserved number."""
        details = details or {}
        entry = self.classes.get(error_class)
        if entry is None:
            return self.unmapped(f"the source has no entry for the error class {error_class!r}")
        if "variants" in entry:
            for variant in entry["variants"]:
                if self._fits(variant, details):
                    return self._rendered(error_class, variant, details)
            return self.unmapped(f"the source's entry for {error_class!r} has no variant for "
                                 f"{ {k: v for k, v in sorted(details.items())} }")
        return self._rendered(error_class, entry, details)

    @staticmethod
    def _fits(variant: dict, details: dict) -> bool:
        """`when` lists the values a detail may have; `when_present` and `when_absent` name details that must be given, or
        must not be."""
        given = lambda key: details.get(key) is not None            # noqa: E731
        return (all(key in details and details[key] in allowed for key, allowed in variant.get("when", {}).items())
                and all(given(k) for k in variant.get("when_present", []))
                and not any(given(k) for k in variant.get("when_absent", [])))

    def _rendered(self, error_class: str, entry: dict, details: dict) -> Rendered:
        message, exact = self._text(error_class, entry, details)
        return Rendered(self._field(error_class, entry, "number", details), self._field(error_class, entry, "severity", details),
                        self._field(error_class, entry, "state", details), message, entry["ends"],
                        entry.get("catchable_at_same_level", True), exact, False)

    def _field(self, error_class: str, entry: dict, name: str, details: dict) -> int:
        value = entry[name]
        if isinstance(value, dict):
            if value["detail"] not in details:
                raise MissingDetail(error_class, [value["detail"]])
            return int(details[value["detail"]])
        return int(value)

    def _text(self, error_class: str, entry: dict, details: dict) -> tuple[str, bool]:
        template = entry["message"]
        missing = [m for m, _ in _PLACEHOLDER.findall(template) if m not in details]
        unnamed = [m for m, spec in _PLACEHOLDER.findall(template)
                   if spec == "type" and m in details and details[m] not in self.type_names]
        if not missing and not unnamed:
            return self._fill(template, details), True
        if "fallback_message" not in entry:
            raise MissingDetail(error_class, missing or unnamed)
        return self._fill(entry["fallback_message"], details), False

    def _fill(self, template: str, details: dict) -> str:
        def one(match: re.Match) -> str:
            name, spec = match.group(1), match.group(2)
            value = details[name]
            return self.type_names[value] if spec == "type" else str(value)
        return _PLACEHOLDER.sub(one, template)

    def unmapped(self, reason: str, original: str | None = None) -> Rendered:
        """The error of a failure the mapping does not cover: the reserved number, and the reason."""
        r = self.reserved
        text = r["message"].replace("{reason}", reason).replace("{original}", original or "")
        return Rendered(int(r["number"]), int(r["severity"]), int(r["state"]), text.strip(), r["ends"], True, False, True,
                        reason)
