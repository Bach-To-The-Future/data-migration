"""The PySpark target's half of the error mapping (ADR 0019; spec section 6, "Errors"): a Spark or Delta failure to a neutral
error class and its details. The rules are data ([`spark-failures.json`]), so the library's version identifies them with the
rest of the library (ADR 0018). This module names no source and no source's number."""
from __future__ import annotations

import dataclasses
import json
import pathlib
import re
from typing import Any

PATH = pathlib.Path(__file__).with_name("spark-failures.json")
FORMAT = "migrt-failure-mapping/1"


@dataclasses.dataclass(frozen=True)
class Classified:
    """A failure of the runtime, as a class. `error_class` is None for a failure no rule covers, with the reason."""
    error_class: str | None
    details: dict[str, Any]
    condition: str | None
    message: str
    reason: str | None = None


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1].replace("''", "'") if text[0] == "'" else text[1:-1]
    return text


def _parts(text: str) -> list[str]:
    """A Spark identifier written with backticks: `a`.`b` is the parts a and b."""
    return [p.replace("``", "`") for p in re.findall(r"`((?:[^`]|``)*)`", text)] or [text]


class SparkFailures:
    def __init__(self, data: dict):
        if data.get("format") != FORMAT:
            raise ValueError(f"the failure mapping has format {data.get('format')!r}; this library reads {FORMAT!r}")
        self.rules: list[dict] = data["rules"]
        self.types: dict[str, str] = data["types"]
        self.maps: dict[str, dict[str, str]] = data["maps"]

    @classmethod
    def load(cls, source: "str | pathlib.Path | dict | None" = None) -> "SparkFailures":
        if isinstance(source, dict):
            return cls(source)
        return cls(json.loads(pathlib.Path(source or PATH).read_text(encoding="utf-8")))

    # ------------------------------------------------------------------ reading a failure

    @staticmethod
    def _facts(exc: BaseException) -> tuple[str | None, dict[str, str], str] | None:
        """(condition, message parameters, text) of a Spark or Delta failure, or None for anything else: a bug in the
        generated code, or in this library, is never an error the program could have caught."""
        try:
            from py4j.protocol import Py4JJavaError
            from pyspark.errors import PySparkException
        except ImportError:                                                   # pragma: no cover - PySpark is always there
            return None
        if isinstance(exc, PySparkException):
            condition = exc.getCondition() if hasattr(exc, "getCondition") else exc.getErrorClass()
            params = exc.getMessageParameters() or {}
            return condition, {str(k): str(v) for k, v in params.items()}, str(exc)
        if isinstance(exc, Py4JJavaError):
            java = exc.java_exception
            first = java
            while java is not None:
                try:
                    condition = java.getCondition()
                except Exception:                                             # noqa: BLE001 - not a SparkThrowable
                    condition = None
                if condition:
                    params = dict(java.getMessageParameters() or {})
                    return str(condition), {str(k): str(v) for k, v in params.items()}, str(java.getMessage())
                java = java.getCause()
            return None, {}, str(first.getMessage()) if first is not None else str(exc)
        return None

    # ------------------------------------------------------------------ classifying it

    def classify(self, exc: BaseException, operation: str | None = None) -> Classified | None:
        """The class of a Spark or Delta failure. None means it is no failure of the runtime at all. `operation` is the kind of
        statement the caller ran, where it decides the class (a drop that finds nothing is not a read that finds nothing)."""
        facts = self._facts(exc)
        if facts is None:
            return None
        condition, params, text = facts
        if condition is None:
            return Classified(None, {}, None, text, "the failure carries no error condition")
        for rule in self.rules:
            if not self._matches(rule, condition, params, operation):
                continue
            details = {name: self._detail(spec, params) for name, spec in rule["details"].items()}
            return Classified(rule["class"], {k: v for k, v in details.items() if v is not None}, condition, text)
        return Classified(None, {}, condition, text, f"no rule maps the condition {condition}"
                          + (f" for the operation {operation}" if operation else ""))

    def _matches(self, rule: dict, condition: str, params: dict[str, str], operation: str | None) -> bool:
        wanted = rule["condition"]
        if not (condition == wanted or (rule.get("prefix") and condition.startswith(wanted + "."))):
            return False
        if rule.get("operation") not in (None, operation):
            return False
        for param, allowed in rule.get("when", {}).items():
            value = _unquote(params.get(param, "")).upper()
            if re.sub(r"\(.*\)$", "", value) not in allowed:
                return False
        return True

    def _detail(self, spec: list[str], params: dict[str, str]) -> Any:
        first, *steps = spec
        value: Any = first.split(":", 1)[1] if first.startswith("constant:") else params.get(first)
        if value is None:
            return None
        for step in steps:
            if step == "unquote":
                value = _unquote(value)
            elif step == "name":
                value = ".".join(p for p in _parts(value) if p != "spark_catalog")
            elif step == "last_name":
                value = _parts(value)[-1]
            elif step == "type":
                key = re.sub(r"\(.*\)$", "", value.strip()).upper()
                value = self.types.get(key, value)
            elif step.startswith("map:"):
                value = self.maps[step[4:]].get(value, value)
            else:                                                             # pragma: no cover - the file is ours
                raise ValueError(f"unknown step {step!r} in the failure mapping")
        return value
