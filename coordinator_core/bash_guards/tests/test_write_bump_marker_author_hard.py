"""marker_present never clears while out_of_repo_write_block is on."""

from __future__ import annotations

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import _write_bump_marker as marker

_FLAG_ENV = "MACHINE_LOCAL_COORDINATOR_FEATURE_OUT_OF_REPO_WRITE_BLOCK"


def _set_flag(monkeypatch, value: str) -> None:
    monkeypatch.setenv(_FLAG_ENV, value)
    machine_profile.reset_cache()


def _gitdir_with_marker(tmp_path, sid="sess-1"):
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    (gitdir / marker.marker_basename(sid)).touch()
    return gitdir


def test_marker_not_present_under_flag(monkeypatch, tmp_path):
    gitdir = _gitdir_with_marker(tmp_path)
    _set_flag(monkeypatch, "on")
    assert marker.marker_present(gitdir, "sess-1") is False


def test_marker_present_with_flag_off(monkeypatch, tmp_path):
    gitdir = _gitdir_with_marker(tmp_path)
    _set_flag(monkeypatch, "off")
    assert marker.marker_present(gitdir, "sess-1") is True


def test_flag_on_returns_before_any_filesystem_read(monkeypatch, tmp_path):
    _set_flag(monkeypatch, "on")

    class Boom:
        def iterdir(self):
            raise AssertionError("filesystem read under flag")

    assert marker.marker_present(Boom(), "sess-1") is False


def test_bump_is_cleared_false_under_flag(monkeypatch, tmp_path):
    gitdir = _gitdir_with_marker(tmp_path)
    monkeypatch.setattr(marker, "resolve_gitdir", lambda cwd=None: gitdir)
    _set_flag(monkeypatch, "off")
    assert marker.bump_is_cleared(str(tmp_path), "sess-1") is True
    _set_flag(monkeypatch, "on")
    assert marker.bump_is_cleared(str(tmp_path), "sess-1") is False


def test_sweep_unchanged_under_flag(monkeypatch, tmp_path):
    gitdir = _gitdir_with_marker(tmp_path)
    _set_flag(monkeypatch, "on")
    assert marker.sweep_stale_markers(gitdir, ["sess-1"]) == 1
    assert not (gitdir / marker.marker_basename("sess-1")).exists()
