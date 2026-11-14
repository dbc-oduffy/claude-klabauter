"""The git-maintenance idle trigger launches owed tiers once, claims the stamp, spawns nothing when
nothing is owed, and the child skips a busy box with a retry backdate."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.git import maintenance_tick as mt


@pytest.fixture(autouse=True)
def _unpaused(monkeypatch):
    """These tests pin the launch itself; the paused behaviour is pinned in test_pack_deletion_pause."""
    from coordinator_core.git import pack_deletion_pause

    monkeypatch.setattr(pack_deletion_pause, "PACK_DELETION_PAUSED", False)


@pytest.fixture(autouse=True)
def _fresh_gate():
    mt._next_check.clear()
    yield
    mt._next_check.clear()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    (path / ".git").mkdir(parents=True)
    return path


@pytest.fixture
def spawned(monkeypatch):
    calls: list = []
    monkeypatch.setattr(mt, "_spawn", lambda r, tiers: calls.append((r, list(tiers))) or True)
    return calls


def _age(common, tier, seconds):
    path = mt.stamp_path(common, tier)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_missing_stamps_launch_both_tiers_once_and_claim(repo, spawned):
    assert mt.on_idle_tick([repo]) is True
    assert spawned == [(repo.resolve(), ["daily", "weekly"])]
    assert mt.stamp_path(repo / ".git", "daily").exists()
    assert mt.stamp_path(repo / ".git", "weekly").exists()
    mt._next_check.clear()
    assert mt.on_idle_tick([repo]) is False
    assert len(spawned) == 1


def test_only_the_stale_tier_is_launched(repo, spawned):
    common = repo / ".git"
    _age(common, "daily", mt.DAILY_INTERVAL_S + 60)
    _age(common, "weekly", mt.WEEKLY_INTERVAL_S - 3600)
    assert mt.on_idle_tick([repo]) is True
    assert spawned[0][1] == ["daily"]


def test_nothing_owed_spawns_nothing(repo, spawned):
    common = repo / ".git"
    _age(common, "daily", 60)
    _age(common, "weekly", 60)
    assert mt.on_idle_tick([repo]) is False
    assert spawned == []


def test_fresh_stamps_with_threshold_crossed_launch_one_daily_child(repo, spawned, monkeypatch):
    from coordinator_core.git import store_hygiene

    common = repo / ".git"
    _age(common, "daily", mt.THRESHOLD_COOLDOWN_S + 60)
    _age(common, "weekly", 60)
    monkeypatch.setattr(store_hygiene, "owed_by_threshold", lambda c: True)
    assert mt.on_idle_tick([repo]) is True
    assert spawned == [(repo.resolve(), ["daily"])]
    assert time.time() - mt.stamp_path(common, "daily").stat().st_mtime < 60


def test_threshold_crossing_inside_the_cooldown_does_not_relaunch(repo, spawned, monkeypatch):
    from coordinator_core.git import store_hygiene

    common = repo / ".git"
    _age(common, "daily", 60)
    _age(common, "weekly", 60)
    monkeypatch.setattr(store_hygiene, "owed_by_threshold", lambda c: True)
    assert mt.on_idle_tick([repo]) is False
    assert spawned == []


def test_fresh_stamps_with_threshold_clear_spawn_nothing(repo, spawned, monkeypatch):
    from coordinator_core.git import store_hygiene

    common = repo / ".git"
    _age(common, "daily", 60)
    _age(common, "weekly", 60)
    monkeypatch.setattr(store_hygiene, "owed_by_threshold", lambda c: False)
    assert mt.on_idle_tick([repo]) is False
    assert spawned == []


def test_stale_stamp_and_threshold_launch_daily_once(repo, spawned, monkeypatch):
    from coordinator_core.git import store_hygiene

    common = repo / ".git"
    _age(common, "daily", mt.DAILY_INTERVAL_S + 60)
    _age(common, "weekly", 60)
    monkeypatch.setattr(store_hygiene, "owed_by_threshold", lambda c: True)
    assert mt.on_idle_tick([repo]) is True
    assert spawned[0][1] == ["daily"]


def test_threshold_probe_failure_falls_back_to_the_stamp(repo, spawned, monkeypatch):
    from coordinator_core.git import store_hygiene

    def boom(c):
        raise OSError("x")

    monkeypatch.setattr(store_hygiene, "owed_by_threshold", boom)
    assert mt.on_idle_tick([repo]) is True
    assert spawned[0][1] == ["daily", "weekly"]


def test_in_process_gate_skips_restat_within_the_check_interval(repo, spawned):
    common = repo / ".git"
    _age(common, "daily", 60)
    _age(common, "weekly", 60)
    mt.on_idle_tick([repo])
    _age(common, "daily", mt.DAILY_INTERVAL_S + 60)
    assert mt.on_idle_tick([repo]) is False
    assert spawned == []


def test_one_launch_per_tick_across_repos(tmp_path, spawned):
    repos = []
    for name in ("a", "b"):
        path = tmp_path / name
        (path / ".git").mkdir(parents=True)
        repos.append(path)
    assert mt.on_idle_tick(repos) is True
    assert len(spawned) == 1


def test_non_git_dir_is_ignored(tmp_path, spawned):
    assert mt.on_idle_tick([tmp_path]) is False
    assert spawned == []


def test_failed_spawn_backdates_so_retry_is_soon_not_a_full_interval(repo, monkeypatch):
    monkeypatch.setattr(mt, "_spawn", lambda r, tiers: False)
    assert mt.on_idle_tick([repo]) is False
    common = repo / ".git"
    age = time.time() - mt.stamp_path(common, "daily").stat().st_mtime
    assert age > mt.DAILY_INTERVAL_S - mt.RETRY_S - 60
    assert age < mt.DAILY_INTERVAL_S


def test_tick_failure_never_escapes(repo, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(mt, "_owed", boom)
    assert mt.on_idle_tick([repo]) is False


def _patch_child(monkeypatch, *, busy, deferred=False, daily=(0, False), weekly_rc=0):
    from coordinator_core.git import store_hygiene
    from coordinator_core.ops import git_maintenance

    ran: list = []
    monkeypatch.setattr(store_hygiene, "_box_busy", lambda: busy)
    monkeypatch.setattr(store_hygiene, "_run_daily_outcome", lambda r: ran.append("daily") or daily)
    monkeypatch.setattr(
        git_maintenance,
        "run_tier",
        lambda r, t: ran.append(t) or SimpleNamespace(deferred="held" if deferred else None, rc=weekly_rc),
    )
    return ran


def test_child_runs_due_tiers_in_order_when_idle(repo, monkeypatch):
    ran = _patch_child(monkeypatch, busy=False)
    assert mt.run_due(repo, ["daily", "weekly"]) == 0
    assert ran == ["daily", "weekly"]


def test_child_skips_busy_box_and_backdates_the_stamp(repo, monkeypatch):
    ran = _patch_child(monkeypatch, busy=True)
    common = repo / ".git"
    _age(common, "daily", 0)
    assert mt.run_due(repo, ["daily"]) == 0
    assert ran == []
    age = time.time() - mt.stamp_path(common, "daily").stat().st_mtime
    assert age > mt.DAILY_INTERVAL_S - mt.RETRY_S - 60


def test_child_backdates_a_deferred_weekly(repo, monkeypatch):
    _patch_child(monkeypatch, busy=False, deferred=True)
    common = repo / ".git"
    _age(common, "weekly", 0)
    mt.run_due(repo, ["weekly"])
    age = time.time() - mt.stamp_path(common, "weekly").stat().st_mtime
    assert age > mt.WEEKLY_INTERVAL_S - mt.RETRY_S - 60


def test_child_backdates_a_deferred_daily(repo, monkeypatch):
    _patch_child(monkeypatch, busy=False, daily=(0, True))
    common = repo / ".git"
    _age(common, "daily", 0)
    mt.run_due(repo, ["daily"])
    age = time.time() - mt.stamp_path(common, "daily").stat().st_mtime
    assert age > mt.DAILY_INTERVAL_S - mt.RETRY_S - 60


def test_child_keeps_the_stamp_of_a_completed_daily(repo, monkeypatch):
    _patch_child(monkeypatch, busy=False, daily=(0, False))
    common = repo / ".git"
    _age(common, "daily", 0)
    mt.run_due(repo, ["daily"])
    assert time.time() - mt.stamp_path(common, "daily").stat().st_mtime < 60


def test_child_backdates_a_failed_weekly(repo, monkeypatch):
    _patch_child(monkeypatch, busy=False, weekly_rc=1)
    common = repo / ".git"
    _age(common, "weekly", 0)
    assert mt.run_due(repo, ["weekly"]) == 1
    age = time.time() - mt.stamp_path(common, "weekly").stat().st_mtime
    assert age > mt.WEEKLY_INTERVAL_S - mt.RETRY_S - 60


def test_no_op_tick_starts_no_process(repo, spawned, monkeypatch):
    import subprocess

    common = repo / ".git"
    _age(common, "daily", 60)
    _age(common, "weekly", 60)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("spawned a process"))
    assert mt.on_idle_tick([repo]) is False
    assert spawned == []


@pytest.mark.parametrize("tier", ["daily", "weekly"])
def test_backdated_tier_is_owed_after_retry_delay_and_not_before(repo, tier):
    common = repo / ".git"
    mt._backdate(common, tier)
    now = time.time()
    assert not mt._owed(common, tier, now + mt.RETRY_S - 120)
    assert mt._owed(common, tier, now + mt.RETRY_S + 120)


def test_main_rejects_bad_usage():
    assert mt.main(["--bogus"]) == 2
