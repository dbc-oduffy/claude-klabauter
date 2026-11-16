"""An explicit run_in_background false reroutes exactly like a calibrated absent key."""

import pytest

from coordinator_core.hooks.support import foreground_dispatch_strip as fds
from pathlib import Path

SID = "test-explicit-false-rerouted-01"


@pytest.fixture
def calibrated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    git_dir = tmp_path / "gitdir"
    monkeypatch.setattr(fds, "_resolve_git_dir", lambda cwd: str(git_dir))
    marker = fds._bg_capable_path(str(git_dir), SID)
    marker.parent.mkdir(parents=True)
    marker.touch()
    return git_dir


@pytest.mark.parametrize("value", [False, "false", "False", " FALSE "])
def test_explicit_false_reroutes_when_calibrated(calibrated, value) -> None:
    ti = {"prompt": "x", "subagent_type": "executor"}
    result = fds.compute_foreground_reroute(value, SID, ti, "/ignored")
    assert result == ("reroute", True, fds._REROUTE_NOTICE)


def test_explicit_false_reroutes_on_first_sight_and_calibrates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git_dir = tmp_path / "gitdir"
    git_dir.mkdir()
    monkeypatch.setattr(fds, "_resolve_git_dir", lambda cwd: str(git_dir))
    real_sid = "5b137c1a-f32c-4c7b-a9a6-d233bcea62b4"
    first = fds.compute_foreground_reroute(False, real_sid, {"prompt": "x"}, "/ignored")
    assert first is not None and first[0] == "reroute" and first[1] is True
    assert fds._is_bg_capable(str(git_dir), real_sid)
    second = fds.compute_foreground_reroute(None, real_sid, {"prompt": "x"}, "/ignored")
    assert second is not None and second[0] == "reroute"


def test_explicit_false_without_prompt_denies(calibrated) -> None:
    result = fds.compute_foreground_reroute(False, SID, {"description": "d"}, "/ignored")
    assert result is not None and result[0] == "deny"


def test_explicit_false_foreground_ok_hatch_still_passes(calibrated) -> None:
    hatch = fds._foreground_ok_path(str(calibrated), SID)
    hatch.touch()
    assert fds.compute_foreground_reroute(False, SID, {"prompt": "x"}, "/ignored") is None


def test_explicit_false_sync_return_type_still_passes(calibrated) -> None:
    ti = {"prompt": "x", "subagent_type": "coordinator:git-commit-agent"}
    assert fds.compute_foreground_reroute(False, SID, ti, "/ignored") is None


def test_explicit_true_passes(calibrated) -> None:
    assert fds.compute_foreground_reroute(True, SID, {"prompt": "x"}, "/ignored") is None


def test_absent_key_calibrated_still_reroutes(calibrated) -> None:
    result = fds.compute_foreground_reroute(None, SID, {"prompt": "x", "subagent_type": "executor"}, "/ignored")
    assert result is not None and result[0] == "reroute" and result[1] is True


def test_absent_key_uncalibrated_still_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fds, "_resolve_git_dir", lambda cwd: str(tmp_path / "gitdir"))
    assert fds.compute_foreground_reroute(None, SID, {"prompt": "x"}, "/ignored") is None
