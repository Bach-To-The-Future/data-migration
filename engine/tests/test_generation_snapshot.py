"""The comparison half of `tools/generation_snapshot.py`: two captures of what generation produces are the same
byte for byte, or every difference is reported. It needs no server, so it runs on the host.

The capture half runs in the sandbox toolbox against the corpus; it is checked by capturing twice and comparing
(see engine/README.md).
"""
import importlib.util
import pathlib

import pytest

TOOL = pathlib.Path(__file__).resolve().parents[2] / "tools" / "generation_snapshot.py"
spec = importlib.util.spec_from_file_location("generation_snapshot", TOOL)
assert spec is not None and spec.loader is not None
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


def capture(directory: pathlib.Path, files: dict[str, bytes]) -> pathlib.Path:
    directory.mkdir()
    for name, content in files.items():
        (directory / name).write_bytes(content)
    return directory


def test_two_captures_with_the_same_files_are_the_same(tmp_path):
    a = capture(tmp_path / "a", {"x.json": b'{"a": 1}\n', "index.json": b"{}\n"})
    b = capture(tmp_path / "b", {"x.json": b'{"a": 1}\n', "index.json": b"{}\n"})

    assert snapshot.compare(a, b) == []
    assert snapshot.main(["compare", str(a), str(b)]) == 0


def test_a_changed_file_is_reported_with_its_name_and_the_line_that_changed(tmp_path):
    a = capture(tmp_path / "a", {"x.json": b'{\n "text": "def f(ss):"\n}\n'})
    b = capture(tmp_path / "b", {"x.json": b'{\n "text": "def g(ss):"\n}\n'})

    [difference] = snapshot.compare(a, b)

    assert "x.json" in difference
    assert 'def f(ss)' in difference and 'def g(ss)' in difference
    assert snapshot.main(["compare", str(a), str(b)]) == 1


def test_a_file_in_only_one_capture_is_reported_whichever_it_is(tmp_path):
    a = capture(tmp_path / "a", {"both.json": b"1\n", "only_a.json": b"1\n"})
    b = capture(tmp_path / "b", {"both.json": b"1\n", "only_b.json": b"1\n"})

    differences = snapshot.compare(a, b)

    assert len(differences) == 2
    assert any("only_a.json" in d and "only in" in d for d in differences)
    assert any("only_b.json" in d and "only in" in d for d in differences)


def test_a_difference_in_line_endings_alone_is_still_a_difference(tmp_path):
    a = capture(tmp_path / "a", {"x.json": b"one\ntwo\n"})
    b = capture(tmp_path / "b", {"x.json": b"one\r\ntwo\r\n"})

    [difference] = snapshot.compare(a, b)

    assert "x.json" in difference and "bytes" in difference


def test_every_difference_is_reported_not_only_the_first(tmp_path):
    a = capture(tmp_path / "a", {"1.json": b"a\n", "2.json": b"a\n", "3.json": b"a\n"})
    b = capture(tmp_path / "b", {"1.json": b"b\n", "2.json": b"a\n", "3.json": b"c\n"})

    differences = snapshot.compare(a, b)

    assert [name for name in ("1.json", "2.json", "3.json") if any(name in d for d in differences)] == ["1.json", "3.json"]


def test_comparing_a_directory_that_does_not_exist_is_an_error_not_an_empty_match(tmp_path):
    a = capture(tmp_path / "a", {"x.json": b"1\n"})

    with pytest.raises(SystemExit):
        snapshot.main(["compare", str(a), str(tmp_path / "missing")])


def test_an_empty_capture_is_not_the_same_as_another_empty_one(tmp_path):
    """A capture that holds nothing means the run captured nothing; two of them matching proves nothing."""
    a = capture(tmp_path / "a", {})
    b = capture(tmp_path / "b", {})

    assert snapshot.main(["compare", str(a), str(b)]) == 1
