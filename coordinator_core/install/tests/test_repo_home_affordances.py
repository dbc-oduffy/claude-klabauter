"""repo_home affordances: shortcuts under tmp_path, Dev Drive advisory text, non-Windows branch."""
from __future__ import annotations

import os

import pytest

from coordinator_core.install import repo_home_affordances as rha
from coordinator_core.install.repo_home_affordances import (
    CONFLICT,
    CREATED,
    EXISTS,
    REPO_HOME_MISSING,
    TARGET_MISSING,
    SettingsKeyMissing,
    dev_drive_advisory,
    ensure_repo_home_shortcuts,
    resolve_repo_home,
)

_REPO = "Q:" + os.sep + "repos"


def _dirs(tmp_path):
    home = tmp_path / "repo_home"
    home.mkdir()
    gem = tmp_path / "group-em"
    gem.mkdir()
    claude = tmp_path / "dot-claude"
    claude.mkdir()
    return home, {"group-em": gem, "claude-home": claude}


def test_creates_shortcuts_and_is_idempotent(tmp_path):
    home, targets = _dirs(tmp_path)
    first = ensure_repo_home_shortcuts(home, targets)
    assert [r.status for r in first] == [CREATED, CREATED]
    for name, target in targets.items():
        assert (home / name / "marker").exists() is False
        (target / "marker").write_text("x")
        assert (home / name / "marker").read_text() == "x"
    second = ensure_repo_home_shortcuts(home, targets)
    assert [r.status for r in second] == [EXISTS, EXISTS]


def test_conflict_is_not_clobbered(tmp_path):
    home, targets = _dirs(tmp_path)
    (home / "group-em").mkdir()
    (home / "group-em" / "keep").write_text("mine")
    results = ensure_repo_home_shortcuts(home, {"group-em": targets["group-em"], "claude-home": targets["claude-home"]})
    by_name = {r.name: r for r in results}
    assert by_name["group-em"].status == CONFLICT
    assert (home / "group-em" / "keep").read_text() == "mine"
    assert by_name["claude-home"].status == CREATED


def test_link_to_other_target_is_conflict(tmp_path):
    home, targets = _dirs(tmp_path)
    ensure_repo_home_shortcuts(home, {"group-em": targets["claude-home"]})
    results = ensure_repo_home_shortcuts(home, {"group-em": targets["group-em"]})
    assert results[0].status == CONFLICT


def test_missing_target_and_missing_home(tmp_path):
    home, targets = _dirs(tmp_path)
    missing = ensure_repo_home_shortcuts(home, {"x": tmp_path / "nope"})
    assert missing[0].status == TARGET_MISSING
    assert not os.path.lexists(home / "x")
    no_home = ensure_repo_home_shortcuts(tmp_path / "absent", targets)
    assert {r.status for r in no_home} == {REPO_HOME_MISSING}


def test_absent_key_is_typed_refusal_naming_key(monkeypatch):
    monkeypatch.setattr(rha, "require_key", lambda key: (_ for _ in ()).throw(SettingsKeyMissing(key)))
    with pytest.raises(SettingsKeyMissing) as exc:
        resolve_repo_home()
    assert exc.value.key == "coordinator.repo_home"
    assert "coordinator.repo_home" in str(exc.value)


def test_key_resolves_to_path(monkeypatch, tmp_path):
    monkeypatch.setattr(rha, "require_key", lambda key: str(tmp_path))
    assert resolve_repo_home() == tmp_path


def test_advisory_not_applicable_off_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(rha, "_is_windows", lambda: False)
    monkeypatch.setattr(rha, "_volume_probe", lambda root: pytest.fail("probe must not run off Windows"))
    assert dev_drive_advisory(tmp_path) is None


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(rha, "_is_windows", lambda: True)
    monkeypatch.setattr(os.path, "splitdrive", lambda p: ("Q:", ""))


def test_advisory_none_on_dev_drive(windows, monkeypatch):
    monkeypatch.setattr(rha, "_volume_probe", lambda root: ("ReFS", True))
    assert dev_drive_advisory(_REPO) is None


def test_advisory_when_flag_read_false(windows, monkeypatch):
    monkeypatch.setattr(rha, "_volume_probe", lambda root: ("NTFS", False))
    text = dev_drive_advisory(_REPO)
    assert text and "not on a Dev Drive" in text and "NTFS" in text and "flag read" in text


def test_advisory_falls_back_to_not_refs_and_says_so(windows, monkeypatch):
    monkeypatch.setattr(rha, "_volume_probe", lambda root: ("NTFS", None))
    text = dev_drive_advisory(_REPO)
    assert text and "filesystem is not ReFS" in text and "flag unreadable" in text


def test_advisory_silent_when_refs_and_flag_unreadable(windows, monkeypatch):
    monkeypatch.setattr(rha, "_volume_probe", lambda root: ("ReFS", None))
    assert dev_drive_advisory(_REPO) is None


def test_advisory_silent_when_probe_fails(windows, monkeypatch):
    def boom(root):
        raise OSError("no volume")

    monkeypatch.setattr(rha, "_volume_probe", boom)
    assert dev_drive_advisory(_REPO) is None


@pytest.mark.skipif(os.name != "nt", reason="real ctypes volume query is Windows-only")
def test_real_probe_runs_on_windows_volume(tmp_path):
    fs_name, flag = rha._volume_probe(os.path.splitdrive(str(tmp_path))[0] + "\\")
    assert fs_name and isinstance(fs_name, str)
    assert flag is None or isinstance(flag, bool)
    assert dev_drive_advisory(tmp_path) is None or "Dev Drive" in dev_drive_advisory(tmp_path)
