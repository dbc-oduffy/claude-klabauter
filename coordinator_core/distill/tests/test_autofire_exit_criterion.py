"""Exit criterion: above-threshold debt and no live run makes the warm idle tick fire distill once."""

from __future__ import annotations

import json
import os
import time

import pytest

from coordinator_core.distill import autofire, autofire_launch
from coordinator_core.distill.autofire_contract import AUTOFIRE_THRESHOLD
from coordinator_core.ops.workflow_fire import fire
from coordinator_core.warm import idle, lifecycle, server

LOG = "## Run r-2026-07-12a\n- archive/specs/2026-07/spec-0.md -> DISTILLED, folded (run: r-2026-07-12a)\n"


class _FakeVersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False


def _repo(tmp_path, specs: int):
    repo = tmp_path / "repo"
    month = repo / "archive" / "specs" / "2026-07"
    month.mkdir(parents=True)
    for i in range(specs):
        (month / f"spec-{i}.md").write_text("x\n")
    (repo / "state").mkdir()
    (repo / "state" / "distillation-log.md").write_text(LOG)
    return repo


@pytest.fixture
def harness(tmp_path, monkeypatch):
    autofire._next_tick.clear()
    autofire._stamps.clear()
    autofire.last_decisions.clear()
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()
    monkeypatch.setattr(idle, "should_demote", lambda **kwargs: False)
    monkeypatch.setattr(idle, "demote_if_idle", lambda **kwargs: None)
    registry = tmp_path / "registry"
    registry.mkdir()
    monkeypatch.setattr(fire, "_registry_dir", lambda cwd=None: registry)

    calls: list[dict] = []

    def fake_fire(script_path, cwd=None, **kwargs):
        calls.append({"script_path": script_path, "cwd": cwd, **kwargs})
        return {"fire_id": f"fid{len(calls)}", "state": "running"}

    monkeypatch.setattr(fire, "fire_workflow", fake_fire)
    ctx = server._ServerContext(
        name="pipe-exit", sid="sid-exit", version_state=_FakeVersionState(), engine_root=tmp_path
    )
    marks: list[int] = []
    monkeypatch.setattr(idle, "mark_invocation", lambda *a, **k: marks.append(1))
    yield ctx, calls, registry, marks, monkeypatch
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()
    autofire._next_tick.clear()
    autofire._stamps.clear()
    autofire.last_decisions.clear()


def _tick(ctx, monkeypatch, repo, *, interval_elapsed: bool):
    monkeypatch.setattr(server._ServerContext, "served_repos", lambda self: [str(repo)])
    if interval_elapsed:
        autofire._next_tick.clear()
    ctx._idle_tick()


def _seed_live_record(registry, repo, fire_id):
    record = {
        "fire_id": fire_id, "state": "running", "pid": os.getpid(), "log_path": "",
        "script_path": str(autofire_launch.fire_script_path(repo)),
        "status_checked_at": time.time(), "log_size_bytes": None, "execution_started": True,
    }
    (registry / f"{fire_id}.json").write_text(json.dumps(record))


def test_above_threshold_fires_once_through_the_real_tick(tmp_path, harness):
    ctx, calls, registry, marks, mp = harness
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 2)

    _tick(ctx, mp, repo, interval_elapsed=True)
    assert len(calls) == 1
    assert calls[0]["cwd"] == str(repo.resolve())
    assert calls[0]["script_path"] == str(autofire_launch.fire_script_path(repo))
    assert calls[0]["wait"] is False
    assert "/distill" in calls[0]["prompt"]
    assert marks, "an autofire launch marks an invocation"

    # Inside the interval: gated, no second fire.
    _tick(ctx, mp, repo, interval_elapsed=False)
    assert len(calls) == 1

    # The launched run is live in the registry: past the interval, still no fire.
    _seed_live_record(registry, repo, "fid1")
    _tick(ctx, mp, repo, interval_elapsed=True)
    assert len(calls) == 1
    assert autofire.last_decisions[repo.resolve()].reason == "run-live"


def test_terminal_run_with_debt_still_above_threshold_fires_again(tmp_path, harness):
    ctx, calls, registry, _marks, mp = harness
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 2)
    _tick(ctx, mp, repo, interval_elapsed=True)
    _seed_live_record(registry, repo, "fid1")
    record = json.loads((registry / "fid1.json").read_text())
    record.update(state="exited", pid=None)
    (registry / "fid1.json").write_text(json.dumps(record))

    _tick(ctx, mp, repo, interval_elapsed=True)
    assert len(calls) == 2


def test_at_threshold_fires_nothing(tmp_path, harness):
    ctx, calls, _registry, marks, mp = harness
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 1)  # LOG harvests spec-0: debt == threshold

    _tick(ctx, mp, repo, interval_elapsed=True)
    _tick(ctx, mp, repo, interval_elapsed=True)
    assert calls == []
    assert marks == []
    assert autofire.last_decisions[repo.resolve()].reason == "below-threshold"
