"""Unit tests for coordinator_core.search.sources_listdir.

Covers parse-and-produce over the accepted `ls` shapes, the decline set
(unsupported flags, multiple operands, glob operands, non-directory/nonexistent
targets, redirection/substitution operands), and the collation behaviour --
parameterized over more than one locale so a byte-sort implementation masquerading
as collation-aware would be caught rather than silently passing.
"""

from __future__ import annotations

import locale
import os

import pytest

from coordinator_core.search.engine import Unanswerable
from coordinator_core.search.sources_listdir import (
    LsSpec,
    parse_ls_segment,
    run,
)


def _locale_available(name: str) -> bool:
    previous = locale.setlocale(locale.LC_COLLATE)
    try:
        locale.setlocale(locale.LC_COLLATE, name)
        return True
    except locale.Error:
        return False
    finally:
        locale.setlocale(locale.LC_COLLATE, previous)


UTF8_LOCALE_CANDIDATES = ["en_US.UTF-8", "en_US.utf8", "English_United States.utf8"]
_AVAILABLE_UTF8_LOCALE = next(
    (name for name in UTF8_LOCALE_CANDIDATES if _locale_available(name)), None
)


# --------------------------------------------------------------------- parsing


def test_parse_bare_ls():
    spec = parse_ls_segment(["ls"])
    assert spec == LsSpec(directory=".", show_all=False)


def test_parse_ls_with_directory():
    spec = parse_ls_segment(["ls", "subdir"])
    assert spec == LsSpec(directory="subdir", show_all=False)


def test_parse_ls_dash_1():
    spec = parse_ls_segment(["ls", "-1", "subdir"])
    assert spec == LsSpec(directory="subdir", show_all=False)


def test_parse_ls_dash_a():
    spec = parse_ls_segment(["ls", "-a", "subdir"])
    assert spec == LsSpec(directory="subdir", show_all=True)


@pytest.mark.parametrize("tokens", [
    ["ls", "-1a", "subdir"],
    ["ls", "-a1", "subdir"],
])
def test_parse_ls_combined_flags(tokens):
    spec = parse_ls_segment(tokens)
    assert spec == LsSpec(directory="subdir", show_all=True)


# --------------------------------------------------------------------- decline


@pytest.mark.parametrize("flag", ["-l", "-R", "-t", "-S", "-r", "-F"])
def test_decline_unsupported_flags(flag):
    with pytest.raises(Unanswerable):
        parse_ls_segment(["ls", flag])


def test_decline_long_color_flag():
    with pytest.raises(Unanswerable):
        parse_ls_segment(["ls", "--color"])


def test_decline_multiple_operands():
    with pytest.raises(Unanswerable):
        parse_ls_segment(["ls", "dir1", "dir2"])


def test_decline_glob_operand():
    with pytest.raises(Unanswerable):
        parse_ls_segment(["ls", "*.py"])


@pytest.mark.parametrize("tokens", [
    ["ls", ">", "out.txt"],
    ["ls", "-1", "subdir", ">>", "out.txt"],
    ["ls", "<", "in.txt"],
    ["ls", "2>", "err.txt"],
    ["ls", "&>", "out.txt"],
    ["ls", "|&"],
    ["ls", "$(whoami)"],
    ["ls", "`whoami`"],
    ["ls", "<(cat f)"],
    ["ls", "$HOME"],
])
def test_decline_redirection_and_substitution(tokens):
    with pytest.raises(Unanswerable):
        parse_ls_segment(tokens)


def test_decline_not_ls():
    with pytest.raises(Unanswerable):
        parse_ls_segment(["cat", "file.txt"])


def test_decline_nonexistent_directory(tmp_path):
    spec = LsSpec(directory="does-not-exist", show_all=False)
    with pytest.raises(Unanswerable):
        run(spec, cwd=str(tmp_path))


def test_decline_file_operand(tmp_path):
    (tmp_path / "plain.txt").write_text("hi", encoding="utf-8")
    spec = LsSpec(directory="plain.txt", show_all=False)
    with pytest.raises(Unanswerable):
        run(spec, cwd=str(tmp_path))


# --------------------------------------------------------------------- listing


def test_run_omits_dotfiles_by_default(tmp_path):
    (tmp_path / "visible.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".hidden").write_text("x", encoding="utf-8")
    spec = LsSpec(directory=".", show_all=False)
    result = run(spec, cwd=str(tmp_path))
    assert result == ["visible.txt"]


def test_run_dash_a_includes_dot_and_dotdot(tmp_path):
    (tmp_path / "visible.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".hidden").write_text("x", encoding="utf-8")
    spec = LsSpec(directory=".", show_all=True)
    result = run(spec, cwd=str(tmp_path))
    assert set(result) == {".", "..", "visible.txt", ".hidden"}


# --------------------------------------------------------------------- collation


@pytest.mark.parametrize("env_locale", [
    "C",
    pytest.param(
        _AVAILABLE_UTF8_LOCALE,
        marks=pytest.mark.skipif(
            _AVAILABLE_UTF8_LOCALE is None,
            reason="no UTF-8 locale installed in this Python on this box",
        ),
    ),
])
def test_collation_matches_locale(tmp_path, monkeypatch, env_locale):
    names = ["banana", "Apple", "_zeta", "apple2"]
    for name in names:
        (tmp_path / name).write_text("x", encoding="utf-8")

    monkeypatch.setenv("LC_ALL", env_locale or "C")
    spec = LsSpec(directory=".", show_all=False)
    result = run(spec, cwd=str(tmp_path))

    if env_locale in (None, "C", "POSIX"):
        expected = sorted(names)
    else:
        previous = locale.setlocale(locale.LC_COLLATE)
        try:
            locale.setlocale(locale.LC_COLLATE, env_locale)
            import functools
            expected = sorted(names, key=functools.cmp_to_key(locale.strcoll))
        finally:
            locale.setlocale(locale.LC_COLLATE, previous)

    assert result == expected


def test_collation_disagrees_between_locales(tmp_path, monkeypatch):
    """C and a UTF-8 locale must produce genuinely DIFFERENT orderings for the
    same directory -- a test that cannot see a disagreement can never report
    one, which is exactly the shape the staff review rejected."""
    if _AVAILABLE_UTF8_LOCALE is None:
        pytest.skip("no UTF-8 locale installed in this Python on this box")

    names = ["banana", "Apple", "_zeta", "apple2"]
    for name in names:
        (tmp_path / name).write_text("x", encoding="utf-8")
    spec = LsSpec(directory=".", show_all=False)

    monkeypatch.setenv("LC_ALL", "C")
    c_result = run(spec, cwd=str(tmp_path))

    monkeypatch.setenv("LC_ALL", _AVAILABLE_UTF8_LOCALE)
    utf8_result = run(spec, cwd=str(tmp_path))

    assert c_result != utf8_result


def test_collation_declines_on_uninstalled_locale(tmp_path, monkeypatch):
    (tmp_path / "a").write_text("x", encoding="utf-8")
    monkeypatch.setenv("LC_ALL", "zz_not_a_real_locale.UTF-8")
    spec = LsSpec(directory=".", show_all=False)
    with pytest.raises(Unanswerable):
        run(spec, cwd=str(tmp_path))


def test_collation_restores_previous_lc_collate(tmp_path, monkeypatch):
    if _AVAILABLE_UTF8_LOCALE is None:
        pytest.skip("no UTF-8 locale installed in this Python on this box")
    (tmp_path / "a").write_text("x", encoding="utf-8")
    before = locale.setlocale(locale.LC_COLLATE)
    monkeypatch.setenv("LC_ALL", _AVAILABLE_UTF8_LOCALE)
    spec = LsSpec(directory=".", show_all=False)
    run(spec, cwd=str(tmp_path))
    after = locale.setlocale(locale.LC_COLLATE)
    assert after == before
