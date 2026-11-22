"""A run alive past `RUN_WALL_TIMEOUT_SECONDS` is halted by the peer-liveness reaper at any
non-terminal stage, whatever its activity, with a reason naming the elapsed seconds and the bound; a
run under the bound is untouched."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.ask_spool import notice
from coordinator_core.ops import warp_run_deliverable as wrd
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir, write_run_stage_at
from coordinator_core.session import core as session_core

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
PEER = "5e1f0000-0000-4000-8000-0000000000b2"
RUN = "run-peer"
OVER = wrd.RUN_WALL_TIMEOUT_SECONDS + 600


def _stamp(age_seconds: float) -> str:
    return datetime.fromtimestamp(time.time() - age_seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    session_core.init(SELF, cwd=str(root))
    session_core.init(PEER, cwd=str(root))
    return root


def _run(repo: Path, *, stage: str, age: float, active: bool = True) -> Path:
    """Run `run-peer` started `age` seconds ago, last active just now when `active`."""
    peer = record_dir(repo, RUN)
    peer.mkdir(parents=True)
    (peer / "manifest.json").write_text(json.dumps({"rows": [], "doc_writes": []}), encoding="utf-8")
    run_token.mint(repo, RUN, "peer.mjs", session_id=PEER)
    write_run_stage_at(peer, stage, now=_stamp(age), session_id=PEER)
    if active:
        data = json.loads((peer / "run-stage.json").read_text(encoding="utf-8"))
        data["last_activity_at"] = _stamp(0)
        (peer / "run-stage.json").write_text(json.dumps(data), encoding="utf-8")
        (peer / "resume.json").write_text("{}", encoding="utf-8")
    else:
        when = time.time() - age
        for entry in peer.iterdir():
            os.utime(entry, (when, when))
    return peer


def _tail(repo: Path) -> bool:
    return wrd._live_peer_tail(repo, RUN, PEER, str(repo))


@pytest.mark.parametrize("stage", ["minted", "staged"])
def test_an_active_run_past_the_wall_bound_is_halted_at_any_stage(repo, stage):
    peer = _run(repo, stage=stage, age=OVER)
    assert _tail(repo) is False
    marker = json.loads((peer / "halted.json").read_text(encoding="utf-8"))
    assert marker["halted"] == wrd.WALL_TIMEOUT
    assert wrd._halted(repo, RUN)


def test_a_run_under_the_wall_bound_is_not_halted(repo):
    peer = _run(repo, stage="staged", age=wrd.RUN_WALL_TIMEOUT_SECONDS - 600)
    assert _tail(repo) is True
    assert not (peer / "halted.json").exists()
    assert wrd._past_wall_timeout(repo, RUN) is None


def test_the_wall_is_four_hours_and_a_9014s_run_is_not_halted(repo):
    assert wrd.RUN_WALL_TIMEOUT_SECONDS == 14400
    peer = _run(repo, stage="staged", age=9014)
    assert wrd._past_wall_timeout(repo, RUN) is None
    assert _tail(repo) is True
    assert not (peer / "halted.json").exists()


def test_the_halt_reason_names_the_elapsed_seconds_and_the_bound(repo):
    peer = _run(repo, stage="staged", age=OVER)
    elapsed = wrd._past_wall_timeout(repo, RUN)
    assert elapsed is not None and elapsed >= OVER
    _tail(repo)
    reason = json.loads((peer / "halted.json").read_text(encoding="utf-8"))["reason"]
    assert f"{wrd.RUN_WALL_TIMEOUT_SECONDS}s" in reason
    assert "alive" in reason and "wall timeout" in reason
    alive = int(reason.split("alive ")[1].split("s,")[0])
    assert OVER <= alive < OVER + 120


def test_the_halt_appends_a_notice_for_the_runs_session(repo):
    _run(repo, stage="minted", age=OVER)
    _tail(repo)
    text = notice.advisory_text(str(repo), PEER)
    assert len(text.splitlines()) == 1
    assert RUN in text


def test_past_wall_timeout_takes_an_explicit_clock(repo):
    _run(repo, stage="staged", age=0)
    assert wrd._past_wall_timeout(repo, RUN) is None
    later = time.time() + wrd.RUN_WALL_TIMEOUT_SECONDS + 60
    assert wrd._past_wall_timeout(repo, RUN, now=later) >= wrd.RUN_WALL_TIMEOUT_SECONDS


def test_a_terminal_stage_run_is_not_wall_halted(repo):
    peer = _run(repo, stage="staged", age=OVER)
    write_run_stage_at(peer, "landed")
    assert wrd._past_wall_timeout(repo, RUN) is None


def test_an_idle_run_past_the_stale_bound_keeps_its_abandoned_marker(repo):
    peer = _run(repo, stage="minted", age=OVER, active=False)
    _tail(repo)
    marker = json.loads((peer / "halted.json").read_text(encoding="utf-8"))
    assert marker["halted"] == wrd.ABANDONED_AT_MINTED


def test_a_run_with_no_parseable_started_at_is_not_wall_halted(repo):
    peer = _run(repo, stage="staged", age=OVER)
    stage_file = peer / "run-stage.json"
    data = json.loads(stage_file.read_text(encoding="utf-8"))
    data["started_at"] = "not-a-time"
    stage_file.write_text(json.dumps(data), encoding="utf-8")
    assert wrd._past_wall_timeout(repo, RUN) is None
    assert _tail(repo) is True


def _resume(peer: Path, stage: str = "staged") -> None:
    write_run_stage_at(peer, "halted")
    write_run_stage_at(peer, stage)


def test_a_same_id_resume_of_an_over_wall_run_is_not_halted(repo):
    peer = _run(repo, stage="staged", age=OVER)
    _resume(peer)
    data = json.loads((peer / "run-stage.json").read_text(encoding="utf-8"))
    assert data["started_at"] < data["wall_started_at"]
    assert wrd._past_wall_timeout(repo, RUN) is None
    assert _tail(repo) is True
    assert not (peer / "halted.json").exists()


def test_a_resumed_run_is_reaped_once_over_the_wall_from_its_resume(repo):
    peer = _run(repo, stage="staged", age=OVER)
    _resume(peer)
    later = time.time() + wrd.RUN_WALL_TIMEOUT_SECONDS + 60
    assert wrd._past_wall_timeout(repo, RUN, now=later) >= wrd.RUN_WALL_TIMEOUT_SECONDS


def test_a_restage_of_a_live_staged_run_does_not_re_arm_the_wall(repo):
    peer = _run(repo, stage="staged", age=OVER)
    write_run_stage_at(peer, "staged")
    data = json.loads((peer / "run-stage.json").read_text(encoding="utf-8"))
    assert "wall_started_at" not in data
    assert wrd._past_wall_timeout(repo, RUN) is not None


def test_a_first_stage_stamp_does_not_re_arm_the_wall(repo):
    peer = _run(repo, stage="minted", age=OVER)
    write_run_stage_at(peer, "staged")
    data = json.loads((peer / "run-stage.json").read_text(encoding="utf-8"))
    assert "wall_started_at" not in data
    assert wrd._past_wall_timeout(repo, RUN) is not None


def test_an_adopted_run_keeps_its_started_at_clock_and_is_halted_again(repo):
    from coordinator_core.ops.dispatch_emit.ask_contract import adopt_run_stage_at

    peer = _run(repo, stage="staged", age=OVER)
    adopt_run_stage_at(peer, PEER)
    assert wrd._past_wall_timeout(repo, RUN) is not None
    _tail(repo)
    assert json.loads((peer / "halted.json").read_text(encoding="utf-8"))["halted"] == wrd.WALL_TIMEOUT
