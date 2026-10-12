"""Tests for the shared home-root predicate (`_home_root`)."""

from __future__ import annotations

import os

import pytest

from coordinator_core.write_guards import _home_root as hr


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "homedir"
    h.mkdir()
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(hr.Path, "home", classmethod(lambda cls: h))
    return h


def test_new_home_file_is_a_home_root_entry(home):
    assert hr.creates_home_root_entry(str(home / "fo.txt"), None) is True


@pytest.mark.parametrize("name", sorted(hr.TOOL_OWNED_DOTFILES))
def test_tool_owned_dotfiles_allowed(home, name):
    assert hr.creates_home_root_entry(str(home / name), None) is False


def test_ten_tool_owned_names():
    assert len(hr.TOOL_OWNED_DOTFILES) == 10


@pytest.mark.skipif(os.name != "nt", reason="casefold applies on Windows")
def test_dotfile_casefolded_on_windows(home):
    assert hr.creates_home_root_entry(str(home / ".GitConfig"), None) is False
    assert hr.creates_home_root_entry(str(home).upper() + "\\fo.txt", None) is True


def test_existing_file_allowed(home):
    (home / "x").write_text("a")
    assert hr.creates_home_root_entry(str(home / "x"), None) is False


def test_subdir_descendant_allowed(home):
    assert hr.creates_home_root_entry(str(home / "sub" / "x"), None) is False
    assert hr.creates_home_root_entry(str(home / ".claude" / "x"), None) is False


def test_relative_with_cwd_home(home):
    assert hr.creates_home_root_entry("fo.txt", str(home)) is True


def test_relative_with_cwd_subdir_allowed(home):
    (home / "sub").mkdir()
    assert hr.creates_home_root_entry("fo.txt", str(home / "sub")) is False


def test_tilde_target(home):
    assert hr.creates_home_root_entry("~/fo.txt", None) is True


@pytest.mark.skipif(os.name != "nt", reason="MSYS drive mounts decode on Windows only")
def test_msys_spelling_of_home(home):
    drive, rest = os.path.splitdrive(str(home))
    msys = "/" + drive[0].lower() + rest.replace("\\", "/") + "/fo.txt"
    assert hr.creates_home_root_entry(msys, None) is True


def test_home_roots_normalises_env_home(tmp_path, monkeypatch):
    monkeypatch.setattr(hr.Path, "home", classmethod(lambda cls: tmp_path / "other"))
    env = {"HOME": str(tmp_path / "h")}
    assert os.path.normcase(os.path.normpath(str(tmp_path / "h"))) in hr.home_roots(env)


def test_unresolvable_or_empty_returns_false(home):
    assert hr.creates_home_root_entry("", None) is False
    assert hr.creates_home_root_entry("   ", str(home)) is False
    assert hr.creates_home_root_entry("fo.txt", None) is False
    assert hr.creates_home_root_entry(None, None) is False  # type: ignore[arg-type]


def test_home_roots_union_dedupes(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setattr(hr.Path, "home", classmethod(lambda cls: b))
    roots = hr.home_roots({"USERPROFILE": str(a), "HOME": str(a)})
    assert len(roots) == 2


def test_target_in_second_home_root(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("USERPROFILE", str(a))
    monkeypatch.setenv("HOME", str(b))
    monkeypatch.setattr(hr.Path, "home", classmethod(lambda cls: a))
    assert hr.creates_home_root_entry(str(b / "fo.txt"), None) is True


def test_deny_reason_register():
    assert "<repo>/scratch/" in hr.DENY_REASON
    assert "scratchpad" in hr.DENY_REASON
    assert len(hr.DENY_REASON.encode()) <= 220
    for marker in (":\\", ":/", "/Users/", "/home/"):
        assert marker not in hr.DENY_REASON
