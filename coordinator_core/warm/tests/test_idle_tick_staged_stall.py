"""The warm idle tick halts and notifies a `staged` run that stopped progressing, once, and leaves a
fresh, in-flight, non-staged or ancient run alone."""

from __future__ import annotations

import json
import os
import time

import pytest

from coordinator_core.ops.dispatch_emit import halted_run, staged_stall
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir, write_run_stage_at
from coordinator_core.warm import breadcrumb, idle, lifecycle, server
from pathlib import Path


class _FakeVersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return self.is_skewed(client_token)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "served"
    path.mkdir()
    return path


@pytest.fixture
def ctx(tmp_path: Path, repo, monkeypatch: pytest.MonkeyPatch):
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()
    monkeypatch.setattr(idle, "should_demote", lambda **kwargs: False)
    monkeypatch.setattr(idle, "demote_if_idle", lambda **kwargs: None)
    monkeypatch.setattr(breadcrumb, "svc_dir", lambda root=None: tmp_path / "svc")
    monkeypatch.setattr(server._ServerContext, "served_repos", lambda self: [repo])
    yield server._ServerContext(
        name="pipe-stall", sid="sid-stall", version_state=_FakeVersionState(), engine_root=tmp_path
    )
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch):
    """Index, agent-in-flight and notification seams; `index` is the live run ids, `sent` the notices."""
    state = {"index": set(), "inflight": False}
    sent: list = []
    monkeypatch.setattr(
        staged_stall.live_run_index, "read", lambda root, ms, **kw: {r: frozenset() for r in state["index"]}
    )
    monkeypatch.setattr(staged_stall, "agent_in_flight", lambda session, cutoff: state["inflight"])
    monkeypatch.setattr(staged_stall, "_notify", lambda target, text: sent.append((target, text)) or "handed-off")
    state["sent"] = sent
    return state


def _run(repo, seams, run_id, *, stage: str = "staged", idle_s=None, session: str | None = "sess-1") -> Path:
    rdir = record_dir(repo, run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    write_run_stage_at(rdir, stage, session_id=session)
    if session is None:
        stage_file = rdir / "run-stage.json"
        data = json.loads(stage_file.read_text(encoding="utf-8"))
        data["session_id"] = None
        stage_file.write_text(json.dumps(data), encoding="utf-8")
    if idle_s is not None:
        old = time.time() - idle_s
        for path in [rdir, *rdir.iterdir()]:
            os.utime(path, (old, old))
    seams["index"].add(run_id)
    return rdir


def _marker(rdir):
    return json.loads((rdir / halted_run.HALTED_FILE).read_text(encoding="utf-8"))


def test_stalled_staged_run_is_halted_and_notified_once(ctx, repo, seams) -> None:
    rdir = _run(repo, seams, "run-stalled", idle_s=staged_stall.STAGED_STALL_BOUND_SECS + 300)
    ctx._idle_tick()
    marker = _marker(rdir)
    assert marker["halted"] == staged_stall.HALT_STAGED_STALL
    assert "idle" in marker["reason"] and "staged run" in marker["reason"]
    assert len(seams["sent"]) == 1
    target, text = seams["sent"][0]
    assert target == "sess-1" and "run-stalled" in text
    ctx._idle_tick()
    assert len(seams["sent"]) == 1


def test_fresh_staged_run_is_left_alone(ctx, repo, seams) -> None:
    rdir = _run(repo, seams, "run-fresh", idle_s=staged_stall.STAGED_STALL_BOUND_SECS - 300)
    ctx._idle_tick()
    assert not (rdir / halted_run.HALTED_FILE).exists()
    assert seams["sent"] == []


def test_run_with_an_agent_in_flight_is_not_halted(ctx, repo, seams) -> None:
    rdir = _run(repo, seams, "run-busy", idle_s=staged_stall.STAGED_STALL_BOUND_SECS + 300)
    seams["inflight"] = True
    ctx._idle_tick()
    assert not (rdir / halted_run.HALTED_FILE).exists()
    assert seams["sent"] == []


@pytest.mark.parametrize("stage", ["minted", "landed"])
def test_run_not_at_staged_is_left_alone(ctx, repo, seams, stage) -> None:
    rdir = _run(repo, seams, "run-other", stage=stage, idle_s=staged_stall.STAGED_STALL_BOUND_SECS + 300)
    ctx._idle_tick()
    assert not (rdir / halted_run.HALTED_FILE).exists()
    assert seams["sent"] == []


def test_run_idle_past_the_horizon_is_history_not_halted(ctx, repo, seams) -> None:
    rdir = _run(repo, seams, "run-ancient", idle_s=staged_stall.STAGED_STALL_HORIZON_SECS + 3600)
    ctx._idle_tick()
    assert not (rdir / halted_run.HALTED_FILE).exists()
    assert seams["sent"] == []


def test_run_outside_the_live_index_is_ignored(ctx, repo, seams) -> None:
    rdir = _run(repo, seams, "run-unindexed", idle_s=staged_stall.STAGED_STALL_BOUND_SECS + 300)
    seams["index"].clear()
    ctx._idle_tick()
    assert not (rdir / halted_run.HALTED_FILE).exists()


def test_run_without_a_session_is_halted_without_a_notice(ctx, repo, seams) -> None:
    rdir = _run(repo, seams, "run-nosess", idle_s=staged_stall.STAGED_STALL_BOUND_SECS + 300, session=None)
    ctx._idle_tick()
    assert (rdir / halted_run.HALTED_FILE).exists()
    assert seams["sent"] == []


def test_scan_failure_never_escapes_the_tick(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(root, ms, **kw):
        raise RuntimeError("index broke")

    monkeypatch.setattr(staged_stall.live_run_index, "read", boom)
    ctx._idle_tick()


def _fire(repo, run_id, *, age_s: float = 0.0, with_agents: bool = True) -> Path:
    """A headless fire record plus journal and agent dirs, all stamped `age_s` seconds old."""
    from coordinator_core.session_hub_path import hub_for_common

    registry = hub_for_common(repo / ".git") / "workflow-fires"
    home = registry / "headless" / "fire1"
    (home / "agents" / "1").mkdir(parents=True, exist_ok=True)
    (home / "journal.jsonl").write_text("{}\n", encoding="utf-8")
    (registry / "fire1.json").write_text(
        json.dumps({"fire_id": "fire1", "run_id": run_id, "artifact_dir": str(home)}), encoding="utf-8"
    )
    old = time.time() - age_s
    for path in (home / "journal.jsonl", home / "agents" / "1", home / "agents"):
        os.utime(path, (old, old))
    return home


def test_run_with_a_progressing_fire_is_not_halted(ctx, repo, seams) -> None:
    rdir = _run(repo, seams, "run-fire", idle_s=staged_stall.STAGED_STALL_BOUND_SECS + 300)
    _fire(repo, "run-fire", age_s=5)
    ctx._idle_tick()
    assert not (rdir / halted_run.HALTED_FILE).exists()
    assert seams["sent"] == []


def test_run_with_a_silent_fire_is_still_halted(ctx, repo, seams) -> None:
    rdir = _run(repo, seams, "run-fire-dead", idle_s=staged_stall.STAGED_STALL_BOUND_SECS + 300)
    _fire(repo, "run-fire-dead", age_s=staged_stall.STAGED_STALL_BOUND_SECS + 300)
    ctx._idle_tick()
    assert _marker(rdir)["halted"] == staged_stall.HALT_STAGED_STALL


def test_stall_halt_is_cleared_when_the_fire_progresses_afterwards(ctx, repo, seams) -> None:
    rdir = _run(repo, seams, "run-late", idle_s=staged_stall.STAGED_STALL_BOUND_SECS + 300)
    ctx._idle_tick()
    assert _marker(rdir)["status"] == halted_run.STATUS_HALTED
    marker_at = (rdir / halted_run.HALTED_FILE).stat().st_mtime
    home = _fire(repo, "run-late", age_s=0)
    os.utime(home / "agents" / "1", (marker_at + 5, marker_at + 5))
    seams["sent"].clear()
    ctx._idle_tick()
    marker = _marker(rdir)
    assert marker["status"] == halted_run.STATUS_RESUMED and marker[halted_run.STALL_CLEARED_FIELD]
    assert json.loads((rdir / "run-stage.json").read_text(encoding="utf-8"))["stage"] == "staged"
    assert len(seams["sent"]) == 1 and "Do not resume or re-fire" in seams["sent"][0][1]
    ctx._idle_tick()
    assert len(seams["sent"]) == 1


def test_cleared_stall_is_live_not_terminal_or_halted(ctx, repo, seams) -> None:
    from coordinator_core.ops.dispatch_emit import run_terminality
    from coordinator_core.ops import warp_run_deliverable

    rdir = _run(repo, seams, "run-live", idle_s=staged_stall.STAGED_STALL_BOUND_SECS + 300)
    ctx._idle_tick()
    assert warp_run_deliverable._halted(repo, "run-live")
    marker_at = (rdir / halted_run.HALTED_FILE).stat().st_mtime
    home = _fire(repo, "run-live", age_s=0)
    os.utime(home / "journal.jsonl", (marker_at + 5, marker_at + 5))
    ctx._idle_tick()
    assert not warp_run_deliverable._halted(repo, "run-live")
    reply = run_terminality._classify_marker("run-live", _marker(rdir))
    assert reply["terminal"] is False and reply["resumable"] is False
    assert halted_run.mark_landed(rdir, "a" * 40)


def test_other_halt_kinds_are_never_cleared(repo) -> None:
    rdir = record_dir(repo, "run-other-halt")
    rdir.mkdir(parents=True)
    halted_run.write_halt_marker(rdir, halted="some_other_halt", sizing_rel=None)
    assert halted_run.mark_stall_cleared(rdir, {}) is False
    assert _marker(rdir)["status"] == halted_run.STATUS_HALTED
