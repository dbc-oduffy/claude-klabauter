"""Explicit run_in_background false is honoured; only an absent key on a calibrated session reroutes."""

import pytest

from coordinator_core.hooks.support import foreground_dispatch_strip as fds

SID = "test-explicit-false-honored-01"


@pytest.fixture
def calibrated(tmp_path, monkeypatch):
    git_dir = tmp_path / "gitdir"
    monkeypatch.setattr(fds, "_resolve_git_dir", lambda cwd: str(git_dir))
    marker = fds._bg_capable_path(str(git_dir), SID)
    marker.parent.mkdir(parents=True)
    marker.touch()
    return git_dir


@pytest.mark.parametrize("value", [False, "false", "False"])
def test_explicit_false_passes_even_when_calibrated(calibrated, value):
    ti = {"prompt": "x", "subagent_type": "executor"}
    assert fds.compute_foreground_reroute(value, SID, ti, "/ignored") is None


def test_explicit_false_still_calibrates(tmp_path, monkeypatch):
    git_dir = tmp_path / "gitdir"
    git_dir.mkdir()
    monkeypatch.setattr(fds, "_resolve_git_dir", lambda cwd: str(git_dir))
    real_sid = "5b137c1a-f32c-4c7b-a9a6-d233bcea62b4"
    assert fds.compute_foreground_reroute(False, real_sid, {"prompt": "x"}, "/ignored") is None
    result = fds.compute_foreground_reroute(None, real_sid, {"prompt": "x"}, "/ignored")
    assert result is not None and result[0] == "reroute"


def test_absent_key_calibrated_still_reroutes(calibrated):
    result = fds.compute_foreground_reroute(None, SID, {"prompt": "x", "subagent_type": "executor"}, "/ignored")
    assert result is not None and result[0] == "reroute" and result[1] is True
