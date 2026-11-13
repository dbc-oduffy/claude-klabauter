"""Tests for autofire.on_idle_tick: threshold decision, live suppression, exactly-once claim."""

from __future__ import annotations

import json
import os
import time

import pytest

from coordinator_core.distill import autofire, autofire_launch
from coordinator_core.distill.autofire_contract import AUTOFIRE_INTERVAL_SECS, AUTOFIRE_THRESHOLD
from coordinator_core.ops.workflow_fire import fire

LOG = "## Run r-2026-07-12a\n- archive/specs/2026-07/spec-0.md -> DISTILLED, folded (run: r-2026-07-12a)\n"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    autofire._next_tick.clear()
    autofire._stamps.clear()
    autofire.last_decisions.clear()
    registry = tmp_path / "registry"
    monkeypatch.setattr(fire, "_registry_dir", lambda cwd=None: registry)
    yield registry


def _repo(tmp_path, specs: int, log: str | None = LOG):
    repo = tmp_path / "repo"
    month = repo / "archive" / "specs" / "2026-07"
    month.mkdir(parents=True, exist_ok=True)
    for i in range(specs):
        (month / f"spec-{i}.md").write_text("x\n")
    if log is not None:
        (repo / "state").mkdir(exist_ok=True)
        (repo / "state" / "distillation-log.md").write_text(log)
    return repo


class Fake:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, repo):
        self.calls.append(repo)
        if self.fail:
            raise RuntimeError("spawn failed")
        return {"fire_id": f"fid{len(self.calls)}", "state": "running"}


def _tick(repo, launcher, t=0.0):
    return autofire.on_idle_tick([repo], clock=lambda: t, launcher=launcher)


def _seed_record(registry, repo, fire_id, state, pid):
    registry.mkdir(parents=True, exist_ok=True)
    record = {
        "fire_id": fire_id, "state": state, "pid": pid, "log_path": "",
        "script_path": str(autofire_launch.fire_script_path(repo)),
        "status_checked_at": time.time(), "log_size_bytes": None, "execution_started": True,
    }
    (registry / f"{fire_id}.json").write_text(json.dumps(record))


def test_above_threshold_fires_once(tmp_path):
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 2)
    fake = Fake()
    assert _tick(repo, fake) is True
    assert fake.calls == [repo.resolve()]
    assert json.loads((repo / autofire.CLAIM_REL).read_text())["fire_id"] == "fid1"


def test_at_and_below_threshold_do_not_fire(tmp_path):
    # LOG harvests spec-0, so N specs leave N-1 debt.
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 1)
    fake = Fake()
    assert _tick(repo, fake) is False
    assert autofire.last_decisions[repo.resolve()].reason == "below-threshold"
    assert autofire.last_decisions[repo.resolve()].debt == AUTOFIRE_THRESHOLD
    assert fake.calls == []


def test_live_record_suppresses(tmp_path, _isolate):
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3)
    _seed_record(_isolate, repo.resolve(), "live1", "running", os.getpid())
    fake = Fake()
    assert _tick(repo, fake) is False
    assert fake.calls == []
    assert autofire.last_decisions[repo.resolve()].reason == "run-live"


def test_running_record_with_dead_pid_does_not_suppress(tmp_path, _isolate):
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3)
    _seed_record(_isolate, repo.resolve(), "dead1", "running", 2**22 + 12345)
    fake = Fake()
    assert _tick(repo, fake) is True
    assert len(fake.calls) == 1


def test_claim_race_one_fire(tmp_path, _isolate):
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3)
    fake = Fake()
    claim = repo / autofire.CLAIM_REL
    claim.parent.mkdir(parents=True)
    claim.write_text("")  # a peer holds the claim, fire_id not yet filled
    assert _tick(repo, fake) is False
    assert fake.calls == []
    claim.unlink()
    assert _tick(repo, fake, t=AUTOFIRE_INTERVAL_SECS + 1) is True
    _seed_record(_isolate, repo.resolve(), "fid1", "running", os.getpid())
    assert _tick(repo, fake, t=2 * AUTOFIRE_INTERVAL_SECS + 2) is False
    assert len(fake.calls) == 1


def test_launch_raising_releases_claim(tmp_path):
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3)
    assert _tick(repo, Fake(fail=True)) is False
    assert not (repo / autofire.CLAIM_REL).exists()
    assert autofire.last_decisions[repo.resolve()].reason == "error:RuntimeError"
    fake = Fake()
    assert _tick(repo, fake, t=AUTOFIRE_INTERVAL_SECS + 1) is True


def test_orphan_claim_past_interval_is_reclaimed(tmp_path):
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3)
    claim = repo / autofire.CLAIM_REL
    claim.parent.mkdir(parents=True)
    claim.write_text("")
    old = time.time() - AUTOFIRE_INTERVAL_SECS - 60
    os.utime(claim, (old, old))
    fake = Fake()
    assert _tick(repo, fake) is True
    assert len(fake.calls) == 1


def test_claim_with_terminal_record_is_released(tmp_path, _isolate):
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3)
    claim = repo / autofire.CLAIM_REL
    claim.parent.mkdir(parents=True)
    claim.write_text(json.dumps({"fire_id": "gone1"}))
    _seed_record(_isolate, repo.resolve(), "gone1", "exited", 1)
    fake = Fake()
    assert _tick(repo, fake) is True


def test_log_absent_is_recorded_not_raised(tmp_path):
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3, log=None)
    fake = Fake()
    assert _tick(repo, fake) is False
    assert autofire.last_decisions[repo.resolve()].reason == "log-absent"
    assert fake.calls == []


def test_stat_gate_skips_recompute(tmp_path, monkeypatch):
    repo = _repo(tmp_path, 1)
    fake = Fake()
    computes = []
    real = autofire.harvest_debt.compute_harvest_debt
    monkeypatch.setattr(
        autofire.harvest_debt, "compute_harvest_debt", lambda *a: computes.append(a) or real(*a)
    )
    _tick(repo, fake, t=0)
    _tick(repo, fake, t=AUTOFIRE_INTERVAL_SECS + 1)
    assert len(computes) == 1
    (repo / "archive" / "specs" / "2026-07" / "new.md").write_text("x\n")
    month = repo / "archive" / "specs" / "2026-07"
    later = time.time() + 5
    os.utime(month, (later, later))
    _tick(repo, fake, t=2 * AUTOFIRE_INTERVAL_SECS + 2)
    assert len(computes) == 2


def test_interval_gate_per_repo(tmp_path):
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3)
    fake = Fake(fail=True)
    _tick(repo, fake, t=0)
    _tick(repo, fake, t=AUTOFIRE_INTERVAL_SECS - 1)
    assert len(fake.calls) == 1


def test_duplicate_and_non_directory_repos_skipped(tmp_path):
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3)
    fake = Fake()
    fired = autofire.on_idle_tick(
        [repo, str(repo / "."), tmp_path / "missing"], clock=lambda: 0.0, launcher=fake
    )
    assert fired is True
    assert len(fake.calls) == 1
