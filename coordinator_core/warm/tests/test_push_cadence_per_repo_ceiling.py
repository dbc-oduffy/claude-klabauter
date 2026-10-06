"""Per-repo push ceiling on the cadence sweep (plan 2026-10-06-the-push-ceiling-resolves-per-repo, C3).

The end-to-end case drives `_sweep_one` -> `push_outstanding` -> `push_with_retry` on a real
repo with a configured upstream; only the streamed git leg and `time.monotonic` are faked.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.push as push_mod
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.ceremony.push_ceiling import PUSH_CEILING_MAX_SECS
from coordinator_core.warm import push_cadence

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    push_cadence.reset_cadence_for_test()
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "author")
    yield
    push_cadence.reset_cadence_for_test()


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _repo_ahead_of_upstream(tmp_path: Path, *, ceiling_secs=None) -> Path:
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(["init", "-q", "--bare", str(bare)], tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.txt").write_text("seed", encoding="utf-8")
    _git(["add", "--", "a.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "work/some-branch"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["push", "-q", "-u", "origin", "work/some-branch"], repo)
    if ceiling_secs is not None:
        _git(["config", "coordinator.pushCeilingSecs", str(ceiling_secs)], repo)
    (repo / "b.txt").write_text("more", encoding="utf-8")
    _git(["add", "--", "b.txt"], repo)
    _git(["commit", "-q", "-m", "second"], repo)
    return repo


class _FakeTime:
    """`push.time` stand-in: a controllable monotonic, everything else real."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


def _install_streamed_leg(monkeypatch, fake_time, *, elapses_secs, stalls=False):
    calls = []

    def _fake_push_streamed(cwd, **kwargs):
        calls.append(kwargs)
        total = kwargs.get("total_timeout")
        if stalls:
            fake_time.now += git_native.STALL_SILENCE_SECS
            return git_native.GitResult(
                returncode=1, stdout="", stderr="stalled\n", stall_killed=True
            )
        if total is not None and total < elapses_secs:
            fake_time.now += total
            return git_native.GitResult(returncode=-1, stdout="", stderr="timed out")
        fake_time.now += elapses_secs
        return git_native.GitResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(push_mod, "time", fake_time)
    monkeypatch.setattr(git_native, "push_streamed", _fake_push_streamed)
    return calls


def test_configured_ceiling_repo_push_completes_past_one_leg_budget(tmp_path, monkeypatch):
    repo = _repo_ahead_of_upstream(tmp_path, ceiling_secs=40)
    fake_time = _FakeTime()
    calls = _install_streamed_leg(monkeypatch, fake_time, elapses_secs=25.0)
    outcomes = []
    real = push_cadence.push_outstanding
    monkeypatch.setattr(
        push_cadence,
        "push_outstanding",
        lambda *a, **k: outcomes.append(real(*a, **k)) or outcomes[-1],
    )

    push_cadence._sweep_one(repo)

    assert len(calls) == 1
    assert calls[0]["total_timeout"] >= 25
    assert calls[0]["total_timeout"] <= PUSH_CEILING_MAX_SECS
    assert calls[0].get("silence_secs", git_native.STALL_SILENCE_SECS) == git_native.STALL_SILENCE_SECS
    assert outcomes[0].acted
    assert not outcomes[0].unconfirmed


def test_unconfigured_repo_keeps_the_default_budget(tmp_path, monkeypatch):
    repo = _repo_ahead_of_upstream(tmp_path)
    fake_time = _FakeTime()
    calls = _install_streamed_leg(monkeypatch, fake_time, elapses_secs=25.0)

    push_cadence._sweep_one(repo)

    assert calls[0]["total_timeout"] <= push_mod.CADENCE_PUSH_RETRY_BUDGET_SECS


def test_stalled_child_dies_on_silence_before_the_configured_ceiling(tmp_path, monkeypatch):
    repo = _repo_ahead_of_upstream(tmp_path, ceiling_secs=40)
    fake_time = _FakeTime()
    start = fake_time.now
    _install_streamed_leg(monkeypatch, fake_time, elapses_secs=0.0, stalls=True)

    push_cadence._sweep_one(repo)

    assert fake_time.now - start < 40.0


# --- sweep_repos admission -------------------------------------------------


def _fake_repos(tmp_path, names):
    roots = []
    for n in names:
        root = tmp_path / n
        (root / ".git").mkdir(parents=True)
        roots.append(root)
    return roots


def _ceilings(monkeypatch, mapping):
    monkeypatch.setattr(
        push_cadence,
        "resolve_push_ceiling",
        lambda root, *, default_secs: mapping.get(Path(root).name, default_secs),
    )


def test_extended_repo_first_with_flag_is_admitted_then_sweep_stops(tmp_path, monkeypatch):
    swept = []
    monkeypatch.setattr(push_cadence, "_sweep_one", lambda root: swept.append(root.name))
    _ceilings(monkeypatch, {"slow": 100.0})
    repos = _fake_repos(tmp_path, ["slow", "b", "c"])

    push_cadence.sweep_repos(repos, allow_extended_first=True, clock=lambda: 0.0)

    assert swept == ["slow"]
    assert push_cadence._resume_repo == repos[1]


def test_extended_repo_in_second_position_is_the_cut(tmp_path, monkeypatch):
    swept = []
    monkeypatch.setattr(push_cadence, "_sweep_one", lambda root: swept.append(root.name))
    _ceilings(monkeypatch, {"slow": 100.0})
    repos = _fake_repos(tmp_path, ["a", "slow", "c"])

    push_cadence.sweep_repos(repos, allow_extended_first=True, clock=lambda: 0.0)

    assert swept == ["a"]
    assert push_cadence._resume_repo == repos[1]
    swept.clear()
    push_cadence.sweep_repos(repos, allow_extended_first=True, clock=lambda: 0.0)
    assert swept == ["slow"]


def test_extended_resume_repo_is_admitted_on_a_real_monotonic_clock(tmp_path, monkeypatch):
    swept = []
    monkeypatch.setattr(push_cadence, "_sweep_one", lambda root: swept.append(root.name))
    _ceilings(monkeypatch, {"slow": 100.0})
    repos = _fake_repos(tmp_path, ["a", "slow", "c"])
    push_cadence._resume_repo = repos[1]

    push_cadence.sweep_repos(repos, allow_extended_first=True, clock=lambda: 50_000.0)

    assert swept == ["slow"]
    assert push_cadence._resume_repo == repos[2]


def test_exit_sweep_declines_an_extended_repo(tmp_path, monkeypatch):
    swept = []
    monkeypatch.setattr(push_cadence, "_sweep_one", lambda root: swept.append(root.name))
    _ceilings(monkeypatch, {"slow": 100.0})
    repos = _fake_repos(tmp_path, ["slow", "b"])

    push_cadence.sweep_repos(
        repos, total_ceiling_secs=push_cadence.EXIT_SWEEP_CEILING_SECS, clock=lambda: 0.0
    )

    assert swept == []
    assert push_cadence._resume_repo == repos[0]


def test_default_ceiling_repos_are_admitted_as_before(tmp_path, monkeypatch):
    swept = []
    monkeypatch.setattr(push_cadence, "_sweep_one", lambda root: swept.append(root.name))
    _ceilings(monkeypatch, {})
    repos = _fake_repos(tmp_path, ["a", "b"])

    push_cadence.sweep_repos(repos, clock=lambda: 0.0)

    assert swept == ["a", "b"]
    assert push_cadence._resume_repo is None


def test_idle_tick_passes_allow_extended_first():
    seen = {}

    def _fake_sweep(repos, **kwargs):
        seen.update(kwargs)

    push_cadence.on_idle_tick(
        served_repos=lambda: [], clock=lambda: 0.0, interval_secs=0.0, sweep_fn=_fake_sweep
    )
    push_cadence.on_idle_tick(
        served_repos=lambda: [], clock=lambda: 1.0, interval_secs=0.0, sweep_fn=_fake_sweep
    )
    assert seen["allow_extended_first"] is True


def test_sweep_lock_hold_covers_the_maximum_ceiling():
    assert push_cadence._SWEEP_LOCK_HOLD_SECS >= PUSH_CEILING_MAX_SECS
