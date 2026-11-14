"""Tests for autofire.on_idle_tick: threshold decision, live suppression, exactly-once claim."""

from __future__ import annotations

import json
import os
import time

import pytest

from coordinator_core.distill import autofire, autofire_launch
from coordinator_core.distill.autofire_contract import (
    AUTOFIRE_INTERVAL_SECS,
    AUTOFIRE_THRESHOLD,
    BACKOFF_MAX_SECS,
)
from coordinator_core.ops.workflow_fire import fire

LOG = "## Run r-2026-07-12a\n- archive/specs/2026-07/spec-0.md -> DISTILLED, folded (run: r-2026-07-12a)\n"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    autofire._next_tick.clear()
    autofire._stamps.clear()
    autofire.last_decisions.clear()
    registry = tmp_path / "registry"
    monkeypatch.setattr(fire, "_registry_dir", lambda cwd=None: registry)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.delenv("COORDINATOR_DISTILL_AUTOFIRE", raising=False)
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
    # Pin directory mtimes: Windows lags directory timestamp updates, which would read as an
    # input change between two stamps taken moments apart.
    pinned = time.time() - 3600
    for directory in (month, month.parent):
        os.utime(directory, (pinned, pinned))
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
    assert _tick(repo, fake) is False
    assert autofire.last_decisions[repo.resolve()].reason == "claim-stampless-released"
    assert not claim.exists()
    assert _tick(repo, fake, t=AUTOFIRE_INTERVAL_SECS + 1) is True
    assert len(fake.calls) == 1


def _terminal_claim(tmp_path, registry, outcome, *, claim_stamp="current", fire_id="gone1"):
    """A claim whose run ended with `outcome`; `claim_stamp` is the at-launch stamp ('current' =
    the repo's stamp now, i.e. no input change since launch)."""
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3).resolve()
    stamp = autofire._normalised_stamp(autofire._input_stamp(repo)) if claim_stamp == "current" else claim_stamp
    claim = repo / autofire.CLAIM_REL
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text(json.dumps({"fire_id": fire_id, "stamp": stamp}))
    _seed_record(registry, repo, fire_id, "exited", None)
    path = registry / f"{fire_id}.json"
    record = json.loads(path.read_text())
    if outcome is not None:
        record["outcome"] = outcome
        record["_annotation_version"] = fire._ANNOTATION_VERSION
    path.write_text(json.dumps(record))
    return repo


def test_claim_with_unknown_outcome_backs_off_not_refires(tmp_path, _isolate):
    repo = _terminal_claim(tmp_path, _isolate, None)
    fake = Fake()
    assert _tick(repo, fake) is False
    assert fake.calls == []
    assert autofire.last_decisions[repo].reason == "backoff:unknown:failures=1"
    assert not (repo / autofire.CLAIM_REL).exists()
    assert json.loads((repo / autofire.BACKOFF_REL).read_text())["failures"] == 1


def test_claim_with_missing_record_backs_off(tmp_path, _isolate):
    repo = _terminal_claim(tmp_path, _isolate, None)
    (_isolate / "gone1.json").unlink()
    assert _tick(repo, Fake()) is False
    assert autofire.last_decisions[repo].reason == "backoff:missing:failures=1"


def test_clean_outcome_with_unchanged_inputs_holds_no_new_input(tmp_path, _isolate):
    """Exit 0 that recorded nothing: the stamp is what proves a log entry landed."""
    repo = _terminal_claim(tmp_path, _isolate, "clean")
    fake = Fake()
    assert _tick(repo, fake) is False
    assert autofire.last_decisions[repo].reason == "no-new-input"


def test_two_ticks_after_completed_run_never_relaunch(tmp_path, _isolate):
    """An empty tick spawns no session: the run completed, debt and inputs unchanged."""
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3).resolve()
    fake = Fake()
    assert _tick(repo, fake, t=0) is True
    assert len(fake.calls) == 1
    _seed_record(_isolate, repo, "fid1", "exited", None)
    path = _isolate / "fid1.json"
    record = json.loads(path.read_text())
    record["outcome"] = "clean"
    record["_annotation_version"] = fire._ANNOTATION_VERSION
    path.write_text(json.dumps(record))
    for n in (1, 2, 3):
        assert _tick(repo, fake, t=n * (AUTOFIRE_INTERVAL_SECS + 1)) is False
        assert autofire.last_decisions[repo].reason == "no-new-input"
    # Even long past any exponential back-off window the hold stands.
    state = _backoff_state(repo)
    state["next_at"] = time.time() - 10_000
    (repo / autofire.BACKOFF_REL).write_text(json.dumps(state))
    assert _tick(repo, fake, t=10 * AUTOFIRE_INTERVAL_SECS) is False
    assert len(fake.calls) == 1


def test_stamp_change_lifts_no_new_input_hold(tmp_path, _isolate):
    repo = _terminal_claim(tmp_path, _isolate, "clean")
    fake = Fake()
    assert _tick(repo, fake) is False
    month = repo / "archive" / "specs" / "2026-07"
    later = time.time() + 5
    os.utime(month, (later, later))
    assert _tick(repo, fake, t=AUTOFIRE_INTERVAL_SECS + 1) is True
    assert len(fake.calls) == 1
    assert not (repo / autofire.BACKOFF_REL).exists()


def test_stampless_claim_with_terminal_run_is_named_and_held(tmp_path, _isolate):
    repo = _terminal_claim(tmp_path, _isolate, "clean", claim_stamp="current")
    claim = repo / autofire.CLAIM_REL
    claim.write_text(json.dumps({"fire_id": "gone1"}))
    fake = Fake()
    assert _tick(repo, fake) is False
    assert autofire.last_decisions[repo].reason == "claim-stampless-released"
    assert not claim.exists()
    assert _tick(repo, fake, t=AUTOFIRE_INTERVAL_SECS + 1) is False
    assert autofire.last_decisions[repo].reason == "no-new-input"
    assert fake.calls == []


def test_stampless_claim_with_live_run_still_blocks(tmp_path, _isolate):
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3).resolve()
    claim = repo / autofire.CLAIM_REL
    claim.parent.mkdir(parents=True)
    claim.write_text(json.dumps({"fire_id": "live1"}))
    _seed_record(_isolate, repo, "live1", "running", os.getpid())
    fake = Fake()
    assert _tick(repo, fake) is False
    assert autofire.last_decisions[repo].reason == "run-live"


def test_clean_outcome_with_changed_inputs_refires(tmp_path, _isolate):
    repo = _terminal_claim(tmp_path, _isolate, "clean", claim_stamp=[1, 2, []])
    fake = Fake()
    assert _tick(repo, fake) is True
    assert len(fake.calls) == 1
    assert not (repo / autofire.BACKOFF_REL).exists()


def test_launch_writes_normalised_stamp_into_claim(tmp_path):
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3)
    assert _tick(repo, Fake()) is True
    stamp = json.loads((repo / autofire.CLAIM_REL).read_text())["stamp"]
    assert stamp == autofire._normalised_stamp(autofire._input_stamp(repo.resolve()))


def _backoff_state(repo):
    return json.loads((repo / autofire.BACKOFF_REL).read_text())


def test_backoff_grows_exponentially_and_caps(tmp_path):
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3).resolve()
    stamp = autofire._input_stamp(repo)
    waits = []
    for _ in range(12):
        before = time.time()
        autofire._record_failure(repo, "f", "unknown", stamp)
        waits.append(round(_backoff_state(repo)["next_at"] - before))
    assert waits[0] == AUTOFIRE_INTERVAL_SECS * 2
    assert waits[1] == AUTOFIRE_INTERVAL_SECS * 4
    assert waits[2] == AUTOFIRE_INTERVAL_SECS * 8
    assert waits[-1] == BACKOFF_MAX_SECS
    assert _backoff_state(repo)["failures"] == 12


def test_standing_backoff_holds_then_expires(tmp_path):
    """The JSON round-trip of the stamp must not read as 'inputs changed'."""
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3).resolve()
    autofire._record_failure(repo, "f", "unknown", autofire._input_stamp(repo))
    fake = Fake()
    assert _tick(repo, fake, t=0) is False
    assert autofire.last_decisions[repo].reason == "backoff:unknown:failures=1"
    assert _tick(repo, fake, t=AUTOFIRE_INTERVAL_SECS + 1) is False
    assert fake.calls == []
    state = _backoff_state(repo)
    state["next_at"] = time.time() - 1
    (repo / autofire.BACKOFF_REL).write_text(json.dumps(state))
    assert _tick(repo, fake, t=2 * AUTOFIRE_INTERVAL_SECS + 2) is True


def test_input_change_clears_backoff(tmp_path):
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3).resolve()
    autofire._record_failure(repo, "f", "unknown", autofire._input_stamp(repo))
    month = repo / "archive" / "specs" / "2026-07"
    later = time.time() + 5
    os.utime(month, (later, later))
    fake = Fake()
    assert _tick(repo, fake) is True
    assert not (repo / autofire.BACKOFF_REL).exists()


def test_kill_switch_env_blocks_every_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISTILL_AUTOFIRE", "off")
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3)
    fake = Fake()
    assert _tick(repo, fake) is False
    assert fake.calls == []
    assert autofire.last_decisions[repo.resolve()].reason == "disabled:kill-switch"


def test_kill_switch_env_unset_values_do_not_block(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISTILL_AUTOFIRE", "1")
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3)
    assert _tick(repo, Fake()) is True


def test_kill_switch_flag_file_blocks_and_removal_rearms(tmp_path):
    flag = tmp_path / "settings-home" / "distill-autofire.off"
    flag.parent.mkdir()
    flag.write_text("")
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3)
    fake = Fake()
    assert _tick(repo, fake) is False
    assert fake.calls == []
    assert autofire.last_decisions[repo.resolve()].reason == "disabled:kill-switch"
    flag.unlink()
    assert _tick(repo, fake, t=1.0) is True


def test_unresolved_settings_home_disables(tmp_path, monkeypatch):
    from coordinator_core import _settings_home

    def boom():
        raise RuntimeError("no home")

    monkeypatch.setattr(_settings_home, "settings_home", boom)
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3)
    fake = Fake()
    assert _tick(repo, fake) is False
    assert fake.calls == []
    assert autofire.last_decisions[repo.resolve()].reason == "disabled:settings-home-unresolved"


def test_stopgap_directory_reads_disabled_not_error(tmp_path):
    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3)
    autofire_launch.fire_script_path(repo).mkdir(parents=True)
    fake = Fake()
    assert _tick(repo, fake) is False
    assert fake.calls == []
    assert autofire.last_decisions[repo.resolve()].reason == "disabled:stopgap-dir"


def test_launcher_raising_autofire_disabled_reads_stopgap(tmp_path):
    from coordinator_core.distill.autofire_contract import AutofireDisabled

    def refuse(repo):
        raise AutofireDisabled("stopgap")

    repo = _repo(tmp_path, AUTOFIRE_THRESHOLD + 3)
    assert _tick(repo, refuse) is False
    assert autofire.last_decisions[repo.resolve()].reason == "disabled:stopgap-dir"
    assert not (repo / autofire.CLAIM_REL).exists()


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
