"""SQL Server's built-in languages: the table the source half holds, and the proof that it is SQL Server's.

Without a server the language is a required setting, and `DATEFIRST` and `DATEFORMAT` are derived from it by this
table (spec section 3, "Language"). The table is data, so a test compares every row with `sys.syslanguages` in the
sandbox: a table that drifted from the server's would assume the wrong week and the wrong date order."""
import pytest

from conftest import sa_connection
from migrt.sources.sqlserver import languages


def test_the_table_holds_exactly_the_rows_of_sys_syslanguages_in_the_sandbox():
    with sa_connection() as sa:
        server = sa.query("SELECT langid, name, alias, dateformat, datefirst FROM sys.syslanguages")

    held = [(lang.langid, lang.name, lang.alias, lang.dateformat, lang.datefirst) for lang in languages.LANGUAGES]
    assert sorted(held) == sorted((int(r["langid"]), r["name"], r["alias"], r["dateformat"], int(r["datefirst"]))
                                  for r in server)


def test_a_language_is_found_by_its_name_or_its_alias_in_any_case():
    for spelling in ("us_english", "US_English", "English", "english"):
        assert languages.find(spelling) is languages.find("us_english")
    german = languages.find("German")
    assert german is languages.find("Deutsch") and german is not None
    assert (german.dateformat, german.datefirst) == ("dmy", 1)
    british = languages.find("British English")
    assert british is languages.find("British") and british is not None
    assert (british.dateformat, british.datefirst) == ("dmy", 1)


def test_us_english_is_mdy_with_the_week_starting_on_sunday():
    us = languages.find("us_english")
    assert us is not None and (us.langid, us.dateformat, us.datefirst) == (0, "mdy", 7)


def test_a_language_the_table_does_not_hold_is_not_found():
    assert languages.find("Klingon") is None
    assert languages.find("") is None


def test_no_two_languages_share_a_name_or_an_alias():
    spellings = [s.casefold() for lang in languages.LANGUAGES for s in {lang.name, lang.alias}]
    assert len(spellings) == len(set(spellings))


@pytest.mark.parametrize("language", languages.LANGUAGES, ids=lambda lang: lang.name)
def test_every_row_has_a_valid_date_order_and_first_day(language):
    assert language.dateformat in {"mdy", "dmy", "ymd", "ydm", "myd", "dym"}
    assert 1 <= language.datefirst <= 7
