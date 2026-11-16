"""SQL Server's built-in languages (spec section 3, "Language").

Every language fixes the `DATEFORMAT` of date parsing and the `DATEFIRST` that `DATEPART(weekday)` and `DATEADD` read.
With a server these come from its `sys.syslanguages`; without one, the language is a required setting and the two
follow from it by this table, the source half's data. `engine/tests/test_languages.py` compares every row with
`sys.syslanguages` in the sandbox, so a table that drifted from SQL Server's is a failing test, not a wrong week.

A language is named by its `name` (`us_english`, `Deutsch`) or its `alias` (`English`, `German`), which is what
`SET LANGUAGE` accepts.
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class Language:
    langid: int
    name: str
    alias: str
    dateformat: str
    datefirst: int


LANGUAGES = (
    Language(0, "us_english", "English", "mdy", 7),
    Language(1, "Deutsch", "German", "dmy", 1),
    Language(2, "Français", "French", "dmy", 1),
    Language(3, "日本語", "Japanese", "ymd", 7),
    Language(4, "Dansk", "Danish", "dmy", 1),
    Language(5, "Español", "Spanish", "dmy", 1),
    Language(6, "Italiano", "Italian", "dmy", 1),
    Language(7, "Nederlands", "Dutch", "dmy", 1),
    Language(8, "Norsk", "Norwegian", "dmy", 1),
    Language(9, "Português", "Portuguese", "dmy", 7),
    Language(10, "Suomi", "Finnish", "dmy", 1),
    Language(11, "Svenska", "Swedish", "ymd", 1),
    Language(12, "čeština", "Czech", "dmy", 1),
    Language(13, "magyar", "Hungarian", "ymd", 1),
    Language(14, "polski", "Polish", "dmy", 1),
    Language(15, "română", "Romanian", "dmy", 1),
    Language(16, "hrvatski", "Croatian", "ymd", 1),
    Language(17, "slovenčina", "Slovak", "dmy", 1),
    Language(18, "slovenski", "Slovenian", "dmy", 1),
    Language(19, "ελληνικά", "Greek", "dmy", 1),
    Language(20, "български", "Bulgarian", "dmy", 1),
    Language(21, "русский", "Russian", "dmy", 1),
    Language(22, "Türkçe", "Turkish", "dmy", 1),
    Language(23, "British", "British English", "dmy", 1),
    Language(24, "eesti", "Estonian", "dmy", 1),
    Language(25, "latviešu", "Latvian", "ymd", 1),
    Language(26, "lietuvių", "Lithuanian", "ymd", 1),
    Language(27, "Português (Brasil)", "Brazilian", "dmy", 7),
    Language(28, "繁體中文", "Traditional Chinese", "ymd", 7),
    Language(29, "한국어", "Korean", "ymd", 7),
    Language(30, "简体中文", "Simplified Chinese", "ymd", 7),
    Language(31, "Arabic", "Arabic", "dmy", 1),
    Language(32, "ไทย", "Thai", "dmy", 7),
    Language(33, "norsk (bokmål)", "Bokmål", "dmy", 1),
)


class UnknownLanguage(Exception):
    """A language that is not one of SQL Server's built-in ones: nothing is read, and no `DATEFIRST` or `DATEFORMAT`
    is guessed for it."""

    def __init__(self, language: str, reason: str | None = None):
        self.language = language
        super().__init__(reason or (
            f"language {language!r} is not one of SQL Server's built-in languages (name or alias, as in "
            "sys.syslanguages), so its DATEFIRST and DATEFORMAT are not known; nothing was read"))


def find(language: str) -> Language | None:
    """The built-in language with this name or alias, in any case, or None."""
    wanted = language.strip().casefold()
    return next((lang for lang in LANGUAGES if wanted in (lang.name.casefold(), lang.alias.casefold())), None)


def require(language: str) -> Language:
    found = find(language)
    if found is None:
        raise UnknownLanguage(language)
    return found
