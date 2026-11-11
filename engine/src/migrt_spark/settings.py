"""The project's settings file, read by session state at startup (spec section 7). It holds the semantic settings, the
runtime identity values and the named settings that stand for marked secrets."""
from __future__ import annotations

import json
import pathlib
from typing import Any

from .errors import SettingNotSet, UnknownSetting

FORMAT = "migrt-settings/1"


class Settings:
    def __init__(self, data: dict | None = None):
        data = data or {}
        if data and data.get("format") != FORMAT:
            raise ValueError(f"the settings file has format {data.get('format')!r}; this library reads {FORMAT!r}")
        self._data = data
        self.semantic: dict[str, Any] = data.get("semantic", {})

    @classmethod
    def load(cls, source: "str | pathlib.Path | dict | None") -> "Settings":
        if source is None:
            return cls()
        if isinstance(source, dict):
            return cls(source)
        return cls(json.loads(pathlib.Path(source).read_text(encoding="utf-8")))

    def secret(self, name: str) -> str:
        entry = self._data.get("secrets", {}).get(name)
        if entry is None:
            raise UnknownSetting(f"The settings file holds no setting named {name!r}.")
        if entry.get("value") is None:
            raise SettingNotSet(f"The setting {name!r} has no value yet: fill it in in settings.json before running.")
        return entry["value"]

    def runtime_identity(self, name: str) -> str:
        entry = self._data.get("runtime_identity", {}).get(name)
        if entry is None:
            raise UnknownSetting(f"The settings file holds no runtime identity value named {name!r}.")
        return entry["override"] if entry.get("override") is not None else entry["default"]
