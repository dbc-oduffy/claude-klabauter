"""blocker_check.evaluate for the five blocker kinds against real record files."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import blocker_check, pending_oracle, stage_wait

pytestmark = pytest.mark.complete

OWNER = "ask-20261123T084702-0bac9d"
SIZING = "state/sizings/owner.yaml"
SHA = "a" * 40


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _marker(root: Path, run_id: str = OWNER, **fields: object) -> None:
    run_dir = root / "state" / "warp" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "halted.json").write_text(json.dumps(fields), encoding="utf-8")


def _eval(root: Path, kind: str, ref: str = OWNER, **extra: object):
    return blocker_check.evaluate(root, {"kind": kind, "ref": ref, **extra})


def test_run_end_without_marker_is_cleared(repo) -> None:
    state, payload = _eval(repo, "run_end")
    assert (state, payload) == ("cleared", {})


def test_run_end_halted_dependents_unstarted_is_dead_and_other_halt_is_live(repo) -> None:
    _marker(repo, status="halted", sizing_rel=SIZING, halted="dependents_unstarted")
    assert _eval(repo, "run_end").state == "dead"
    _marker(repo, status="halted", sizing_rel=SIZING, halted="commit")
    verdict = _eval(repo, "run_end")
    assert verdict.state == "live"
    assert verdict.payload["sizing_rel"] == SIZING


def test_run_end_superseded_without_landed_successor_is_dead(repo) -> None:
    _marker(repo, status="superseded", sizing_rel=SIZING)
    assert _eval(repo, "run_end").state == "dead"


def test_run_end_resumed_is_pending_and_corrupt_marker_is_live(repo) -> None:
    _marker(repo, status="resumed", sizing_rel=SIZING)
    assert _eval(repo, "run_end").state == "pending"
    other = "ask-20261123T084702-bbbbbb"
    run_dir = repo / "state" / "warp" / other
    run_dir.mkdir(parents=True)
    (run_dir / "halted.json").write_text("{not json", encoding="utf-8")
    assert _eval(repo, "run_end", other).state == "live"


def test_run_landed_clears_only_with_status_landed_and_a_40_hex_sha(repo) -> None:
    _marker(repo, status="landed", sha=SHA)
    verdict = _eval(repo, "run_landed")
    assert verdict == ("cleared", {"sha": SHA})
    assert verdict.cleared is True


@pytest.mark.parametrize("sha", [None, "abc123", "g" * 40, "A" * 40], ids=["absent", "short", "nonhex", "upper"])
def test_run_landed_without_a_valid_sha_is_pending(repo, sha) -> None:
    fields = {"status": "landed"} if sha is None else {"status": "landed", "sha": sha}
    _marker(repo, **fields)
    assert _eval(repo, "run_landed").state == "pending"


def test_run_landed_unreadable_marker_is_pending_and_dead_owner_is_dead(repo) -> None:
    run_dir = repo / "state" / "warp" / OWNER
    run_dir.mkdir(parents=True)
    (run_dir / "halted.json").write_text("[]", encoding="utf-8")
    assert _eval(repo, "run_landed").state == "pending"
    _marker(repo, status="halted", sizing_rel=SIZING, halted="dependents_unstarted")
    assert _eval(repo, "run_landed").state == "dead"


def test_run_landed_with_no_marker_is_pending_not_cleared(repo) -> None:
    assert _eval(repo, "run_landed").state == "pending"


def test_file_state_exists_absent_and_mtime(repo) -> None:
    assert _eval(repo, "file_state", "out/x.txt").state == "live"
    assert _eval(repo, "file_state", "out/x.txt", expect="absent").state == "cleared"
    target = repo / "out" / "x.txt"
    target.parent.mkdir()
    target.write_text("x", encoding="utf-8")
    assert _eval(repo, "file_state", "out/x.txt").state == "cleared"
    assert _eval(repo, "file_state", "out/x.txt", expect="absent").state == "live"
    mtime = target.stat().st_mtime
    assert _eval(repo, "file_state", "out/x.txt", mtime_after=mtime).state == "live"
    assert _eval(repo, "file_state", "out/x.txt", mtime_after=mtime - 10).state == "cleared"


@pytest.mark.parametrize("ref", ["../outside.txt", "/etc/passwd", "C:/Windows/win.ini", "a/../../b"])
def test_file_state_refuses_a_path_outside_the_repo(repo, ref) -> None:
    assert _eval(repo, "file_state", ref, expect="absent").state == "pending"


def test_oracle_check_clears_when_the_flag_file_is_gone(repo) -> None:
    flag = pending_oracle.flag_path(repo, OWNER, "row1")
    flag.parent.mkdir(parents=True)
    flag.write_text("{}", encoding="utf-8")
    assert _eval(repo, "oracle_check", row="row1").state == "live"
    flag.unlink()
    assert _eval(repo, "oracle_check", row="row1").state == "cleared"
    assert _eval(repo, "oracle_check").state == "pending"


def test_remote_run_reads_the_cached_result_and_never_checks_inline(repo, monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    assert _eval(repo, "remote_run", "box9-run1").state == "pending"
    path = blocker_check.remote_result_path(repo, "box9-run1")
    path.parent.mkdir(parents=True, exist_ok=True)
    for status, state in (("running", "live"), ("ended", "cleared"), ("failed", "dead"), ("weird", "pending")):
        path.write_text(json.dumps({"status": status}), encoding="utf-8")
        assert _eval(repo, "remote_run", "box9-run1").state == state
    path.write_text("{nope", encoding="utf-8")
    assert _eval(repo, "remote_run", "box9-run1").state == "pending"
    assert blocker_check.remote_result_path(repo, "../x") is None


def test_unknown_kind_raises_and_missing_ref_is_pending(repo) -> None:
    with pytest.raises(ValueError):
        blocker_check.evaluate(repo, {"kind": "bogus", "ref": "x"})
    assert blocker_check.evaluate(repo, {"kind": "run_end"}).state == "pending"


def test_stage_wait_landed_cleared_reads_the_evaluator(repo) -> None:
    assert stage_wait._landed_cleared(repo, OWNER) is False
    _marker(repo, status="landed", sha=SHA)
    assert stage_wait._landed_cleared(repo, OWNER) is True
