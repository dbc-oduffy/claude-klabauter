"""Tests for the shared home-root predicate (`_home_root`)."""

from __future__ import annotations

import os

import pytest

from coordinator_core.write_guards import _home_root as hr
from pathlib import Path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "homedir"
    h.mkdir()
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(hr.Path, "home", classmethod(lambda cls: h))
    return h


def test_new_home_file_is_a_home_root_entry(home) -> None:
    assert hr.creates_home_root_entry(str(home / "fo.txt"), None) is True


@pytest.mark.parametrize("name", sorted(hr.TOOL_OWNED_DOTFILES))
def test_tool_owned_dotfiles_allowed(home, name) -> None:
    assert hr.creates_home_root_entry(str(home / name), None) is False


def test_ten_tool_owned_names() -> None:
    assert len(hr.TOOL_OWNED_DOTFILES) == 10


@pytest.mark.skipif(os.name != "nt", reason="casefold applies on Windows")
def test_dotfile_casefolded_on_windows(home) -> None:
    assert hr.creates_home_root_entry(str(home / ".GitConfig"), None) is False
    assert hr.creates_home_root_entry(str(home).upper() + "\\fo.txt", None) is True


def test_existing_file_allowed(home) -> None:
    (home / "x").write_text("a")
    assert hr.creates_home_root_entry(str(home / "x"), None) is False


def test_subdir_descendant_allowed(home) -> None:
    assert hr.creates_home_root_entry(str(home / "sub" / "x"), None) is False
    assert hr.creates_home_root_entry(str(home / ".claude" / "x"), None) is False


def test_relative_with_cwd_home(home) -> None:
    assert hr.creates_home_root_entry("fo.txt", str(home)) is True


def test_relative_with_cwd_subdir_allowed(home) -> None:
    (home / "sub").mkdir()
    assert hr.creates_home_root_entry("fo.txt", str(home / "sub")) is False


def test_tilde_target(home) -> None:
    assert hr.creates_home_root_entry("~/fo.txt", None) is True


@pytest.mark.skipif(os.name != "nt", reason="MSYS drive mounts decode on Windows only")
def test_msys_spelling_of_home(home) -> None:
    drive, rest = os.path.splitdrive(str(home))
    msys = "/" + drive[0].lower() + rest.replace("\\", "/") + "/fo.txt"
    assert hr.creates_home_root_entry(msys, None) is True


def test_home_roots_normalises_env_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hr.Path, "home", classmethod(lambda cls: tmp_path / "other"))
    env = {"HOME": str(tmp_path / "h")}
    assert os.path.normcase(os.path.normpath(str(tmp_path / "h"))) in hr.home_roots(env)


def test_unresolvable_or_empty_returns_false(home) -> None:
    assert hr.creates_home_root_entry("", None) is False
    assert hr.creates_home_root_entry("   ", str(home)) is False
    assert hr.creates_home_root_entry("fo.txt", None) is False
    assert hr.creates_home_root_entry(None, None) is False  # type: ignore[arg-type]


def test_home_roots_union_dedupes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setattr(hr.Path, "home", classmethod(lambda cls: b))
    roots = hr.home_roots({"USERPROFILE": str(a), "HOME": str(a)})
    assert len(roots) == 2


def test_target_in_second_home_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("USERPROFILE", str(a))
    monkeypatch.setenv("HOME", str(b))
    monkeypatch.setattr(hr.Path, "home", classmethod(lambda cls: a))
    assert hr.creates_home_root_entry(str(b / "fo.txt"), None) is True


@pytest.mark.parametrize(
    "target",
    ["/h/.claude.json", "h\\sub\\.claude.json.tmp.123588.910255f9", "~/.CLAUDE.JSON.lock"],
)
def test_claude_json_family_is_attributed_to_claude_code(target) -> None:
    assert hr.writer_of(target) == "Claude Code"
    reason = hr.deny_reason(target)
    assert "Claude Code" in reason
    assert reason.startswith("DENY: ") and "Instead: write under <repo>/scratch/" in reason


@pytest.mark.parametrize("target", ["/h/fo.txt", "/h/.claude.jsonx", "/h/.claude", ""])
def test_other_names_keep_the_unattributed_reason(target) -> None:
    assert hr.writer_of(target) is None
    assert hr.deny_reason(target) == hr.DENY_REASON


def test_deny_reason_register() -> None:
    assert "<repo>/scratch/" in hr.DENY_REASON
    assert "scratchpad" in hr.DENY_REASON
    assert len(hr.DENY_REASON.encode()) <= 220
    for marker in (":\\", ":/", "/Users/", "/home/"):
        assert marker not in hr.DENY_REASON
