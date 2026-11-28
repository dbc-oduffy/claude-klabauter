"""background.check_remote_runs: a parked `remote_run` blocker is checked through box_transport and cached for blocker_check."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.group_em import box_transport
from coordinator_core.ops.dispatch_emit import blocker_check, stage_wait
from coordinator_core.remote_engine import background, policy

pytestmark = pytest.mark.complete

SSH = {"kind": "ssh", "host": "h", "invoke": "/x/coordinator-invoke"}
REF = "ask-20261127T000000-aaaaaa"
SHA = "b" * 40


class Runner:
    def __init__(self, stdout: str = "", rc: int = 0) -> None:
        self.calls: list = []
        self.stdout = stdout or json.dumps({"result": {"status": "ended", "sha": SHA}})
        self.rc = rc

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, "")


def _box(box_id: str) -> dict:
    return {"box_id": box_id, "root": "/r", "capability": {}, "transport": SSH}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(box_transport, "_registered", lambda box, host, invoke: None)
    monkeypatch.setattr(policy, "remote_safe", lambda op: True)
    box_transport._reset_limits()
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _park(root: Path, run_id: str = "waiter", ref: str = REF, box: str = "far") -> None:
    stage_wait.park(root, run_id, [{"kind": "remote_run", "ref": ref, "box": box}], [])


def _cached(root: Path, ref: str = REF) -> dict:
    return json.loads(blocker_check.remote_result_path(root, ref).read_text(encoding="utf-8"))


def test_due_blocker_is_asked_once_and_cached_where_blocker_check_reads(repo) -> None:
    _park(repo)
    runner = Runner()
    assert background.check_remote_runs(repo, runner=runner, boxes=[_box("far")], now=1000.0) == 1
    assert _cached(repo) == {"status": "ended", "sha": SHA, "box": "far", "ref": REF, "checked_at": 1000.0}
    assert "dispatch.stage_wait_status" in " ".join(runner.calls[0][0])
    assert blocker_check.evaluate(repo, {"kind": "remote_run", "ref": REF}).state == "cleared"


def test_running_result_is_not_asked_again_before_the_interval(repo) -> None:
    _park(repo)
    runner = Runner(json.dumps({"result": {"status": "running"}}))
    boxes = [_box("far")]
    background.check_remote_runs(repo, runner=runner, boxes=boxes, now=1000.0)
    assert background.check_remote_runs(repo, runner=runner, boxes=boxes, now=1000.0 + background.REMOTE_CHECK_INTERVAL_S - 1) == 0
    assert background.check_remote_runs(repo, runner=runner, boxes=boxes, now=1000.0 + background.REMOTE_CHECK_INTERVAL_S) == 1
    assert len(runner.calls) == 2


def test_a_terminal_result_is_final(repo) -> None:
    _park(repo)
    runner = Runner(json.dumps({"result": {"status": "halted"}}))
    background.check_remote_runs(repo, runner=runner, boxes=[_box("far")], now=1000.0)
    assert background.check_remote_runs(repo, runner=runner, boxes=[_box("far")], now=10**6) == 0
    assert blocker_check.evaluate(repo, {"kind": "remote_run", "ref": REF}).state == "dead"


def test_transport_failure_caches_unknown_and_stays_pending(repo) -> None:
    _park(repo)
    background.check_remote_runs(repo, runner=Runner("", rc=255), boxes=[_box("far")], now=1000.0)
    assert _cached(repo)["status"] == "unknown"
    assert blocker_check.evaluate(repo, {"kind": "remote_run", "ref": REF}).state == "pending"


def test_unregistered_box_makes_no_call(repo) -> None:
    _park(repo, box="gone")
    runner = Runner()
    assert background.check_remote_runs(repo, runner=runner, boxes=[_box("far")], now=1000.0) == 0
    assert runner.calls == []


def test_calls_per_box_per_pass_are_capped(repo) -> None:
    refs = [f"ask-20261127T00000{i}-aaaaaa" for i in range(5)]
    for i, ref in enumerate(refs):
        _park(repo, run_id=f"waiter{i}", ref=ref)
    runner = Runner()
    assert background.check_remote_runs(repo, runner=runner, boxes=[_box("far")], now=1000.0) == background.REMOTE_BOX_BATCH
    assert background.REMOTE_BOX_BATCH <= box_transport.RATE_MAX_CALLS


def test_cache_with_no_wait_record_is_dropped(repo) -> None:
    _park(repo)
    background.check_remote_runs(repo, runner=Runner(), boxes=[_box("far")], now=1000.0)
    path = blocker_check.remote_result_path(repo, REF)
    stage_wait.clear(repo, "waiter")
    background.check_remote_runs(repo, runner=Runner(), boxes=[_box("far")], now=2000.0)
    assert not path.exists()


def test_non_remote_blockers_cost_no_call(repo) -> None:
    stage_wait.park(repo, "waiter", ["ask-20261127T000000-bbbbbb"], [])
    runner = Runner()
    assert background.check_remote_runs(repo, runner=runner, boxes=[_box("far")], now=1000.0) == 0
    assert runner.calls == []


def test_drain_pending_checks_only_when_given_a_root(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []
    monkeypatch.setattr(background, "check_remote_runs", lambda root, **kw: seen.append(root) or 0)
    background.drain_pending()
    assert seen == []
    background.drain_pending(root=repo)
    assert seen == [repo]
