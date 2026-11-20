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
def _unpaused(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests pin the launch itself; the paused behaviour is pinned in test_pack_deletion_pause."""
    from coordinator_core.git import pack_deletion_pause

    monkeypatch.setattr(pack_deletion_pause, "PACK_DELETION_PAUSED", False)


@pytest.fixture(autouse=True)
def _fresh_gate():
    mt._next_check.clear()
    mt._next_paused_check.clear()
    mt._registry_next = float("-inf")
    mt._registry_cache = []
    yield
    mt._next_check.clear()
    mt._next_paused_check.clear()
    mt._registry_next = float("-inf")
    mt._registry_cache = []


@pytest.fixture(autouse=True)
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from coordinator_core.git import git_objects

    path = tmp_path / "engine-read-stores"
    monkeypatch.setattr(git_objects, "read_store_registry_dir", lambda: path)
    monkeypatch.setattr(mt, "_temp_root", lambda: tmp_path / "system-temp")
    monkeypatch.setattr(git_objects, "_RECORDED_READ_STORES", set())
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    (path / ".git").mkdir(parents=True)
    return path


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch):
    calls: list = []
    monkeypatch.setattr(mt, "_spawn", lambda r, tiers: calls.append((r, list(tiers))) or True)
    return calls


def _age(common, tier, seconds) -> None:
    path = mt.stamp_path(common, tier)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_missing_stamps_launch_both_tiers_once_and_claim(repo, spawned) -> None:
    assert mt.on_idle_tick([repo]) is True
    assert spawned == [(repo.resolve(), ["daily", "weekly"])]
    assert mt.stamp_path(repo / ".git", "daily").exists()
    assert mt.stamp_path(repo / ".git", "weekly").exists()
    mt._next_check.clear()
    assert mt.on_idle_tick([repo]) is False
    assert len(spawned) == 1


def test_only_the_stale_tier_is_launched(repo, spawned) -> None:
    common = repo / ".git"
    _age(common, "daily", mt.DAILY_INTERVAL_S + 60)
    _age(common, "weekly", mt.WEEKLY_INTERVAL_S - 3600)
    assert mt.on_idle_tick([repo]) is True
    assert spawned[0][1] == ["daily"]


def test_nothing_owed_spawns_nothing(repo, spawned) -> None:
    common = repo / ".git"
    _age(common, "daily", 60)
    _age(common, "weekly", 60)
    assert mt.on_idle_tick([repo]) is False
    assert spawned == []


def test_fresh_stamps_with_threshold_crossed_launch_one_daily_child(repo, spawned, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.git import store_hygiene

    common = repo / ".git"
    _age(common, "daily", mt.THRESHOLD_COOLDOWN_S + 60)
    _age(common, "weekly", 60)
    monkeypatch.setattr(store_hygiene, "owed_by_threshold", lambda c: True)
    assert mt.on_idle_tick([repo]) is True
    assert spawned == [(repo.resolve(), ["daily"])]
    assert time.time() - mt.stamp_path(common, "daily").stat().st_mtime < 60


def test_threshold_crossing_inside_the_cooldown_does_not_relaunch(repo, spawned, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.git import store_hygiene

    common = repo / ".git"
    _age(common, "daily", 60)
    _age(common, "weekly", 60)
    monkeypatch.setattr(store_hygiene, "owed_by_threshold", lambda c: True)
    assert mt.on_idle_tick([repo]) is False
    assert spawned == []


def test_fresh_stamps_with_threshold_clear_spawn_nothing(repo, spawned, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.git import store_hygiene

    common = repo / ".git"
    _age(common, "daily", 60)
    _age(common, "weekly", 60)
    monkeypatch.setattr(store_hygiene, "owed_by_threshold", lambda c: False)
    assert mt.on_idle_tick([repo]) is False
    assert spawned == []


def test_stale_stamp_and_threshold_launch_daily_once(repo, spawned, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.git import store_hygiene

    common = repo / ".git"
    _age(common, "daily", mt.DAILY_INTERVAL_S + 60)
    _age(common, "weekly", 60)
    monkeypatch.setattr(store_hygiene, "owed_by_threshold", lambda c: True)
    assert mt.on_idle_tick([repo]) is True
    assert spawned[0][1] == ["daily"]


def test_threshold_probe_failure_falls_back_to_the_stamp(repo, spawned, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.git import store_hygiene

    def boom(c):
        raise OSError("x")

    monkeypatch.setattr(store_hygiene, "owed_by_threshold", boom)
    assert mt.on_idle_tick([repo]) is True
    assert spawned[0][1] == ["daily", "weekly"]


def test_in_process_gate_skips_restat_within_the_check_interval(repo, spawned) -> None:
    common = repo / ".git"
    _age(common, "daily", 60)
    _age(common, "weekly", 60)
    mt.on_idle_tick([repo])
    _age(common, "daily", mt.DAILY_INTERVAL_S + 60)
    assert mt.on_idle_tick([repo]) is False
    assert spawned == []


def test_one_launch_per_tick_across_repos(tmp_path: Path, spawned) -> None:
    repos = []
    for name in ("a", "b"):
        path = tmp_path / name
        (path / ".git").mkdir(parents=True)
        repos.append(path)
    assert mt.on_idle_tick(repos) is True
    assert len(spawned) == 1


def test_non_git_dir_is_ignored(tmp_path: Path, spawned) -> None:
    assert mt.on_idle_tick([tmp_path]) is False
    assert spawned == []


def test_failed_spawn_backdates_so_retry_is_soon_not_a_full_interval(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mt, "_spawn", lambda r, tiers: False)
    assert mt.on_idle_tick([repo]) is False
    common = repo / ".git"
    age = time.time() - mt.stamp_path(common, "daily").stat().st_mtime
    assert age > mt.DAILY_INTERVAL_S - mt.RETRY_S - 60
    assert age < mt.DAILY_INTERVAL_S


def test_tick_failure_never_escapes(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(mt, "_owed", boom)
    assert mt.on_idle_tick([repo]) is False


def _patch_child(monkeypatch, *, busy, deferred: bool = False, daily=(0, False), weekly_rc: int = 0):
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


def test_child_runs_due_tiers_in_order_when_idle(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _patch_child(monkeypatch, busy=False)
    assert mt.run_due(repo, ["daily", "weekly"]) == 0
    assert ran == ["daily", "weekly"]


def test_child_skips_busy_box_for_weekly_and_backdates_the_stamp(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _patch_child(monkeypatch, busy=True)
    common = repo / ".git"
    _age(common, "weekly", 0)
    assert mt.run_due(repo, ["weekly"]) == 0
    assert ran == []
    age = time.time() - mt.stamp_path(common, "weekly").stat().st_mtime
    assert age > mt.WEEKLY_INTERVAL_S - mt.RETRY_S - 60


def test_busy_box_still_runs_daily_outcome_and_keeps_its_stamp(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _patch_child(monkeypatch, busy=True)
    common = repo / ".git"
    _age(common, "daily", 0)
    _age(common, "weekly", 0)
    assert mt.run_due(repo, ["daily", "weekly"]) == 0
    assert ran == ["daily"]
    assert time.time() - mt.stamp_path(common, "daily").stat().st_mtime < 60
    age = time.time() - mt.stamp_path(common, "weekly").stat().st_mtime
    assert age > mt.WEEKLY_INTERVAL_S - mt.RETRY_S - 60


def _register(registry: Path, common: Path, name: str = "e") -> None:
    registry.mkdir(parents=True, exist_ok=True)
    (registry / name).write_text(str(common) + "\n", encoding="utf-8")


def test_a_non_served_store_the_reader_opened_is_offered(tmp_path: Path, registry, spawned) -> None:
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    _register(registry, other / ".git")
    assert mt.on_idle_tick([]) is True
    assert spawned == [(other.resolve(), ["daily", "weekly"])]


def test_the_reader_records_each_store_once_per_process(tmp_path: Path, registry) -> None:
    from coordinator_core.git import git_objects

    common = tmp_path / "r" / ".git"
    (common / "objects" / "pack").mkdir(parents=True)
    git_objects._iter_pack_files(common)
    git_objects._PACK_LISTING_CACHE.pop(str(common), None)
    git_objects._iter_pack_files(common)
    entries = list(registry.iterdir())
    assert len(entries) == 1
    assert entries[0].read_text(encoding="utf-8").strip() == str(common)
    assert mt._read_store_repos() == [common.parent]


def test_registry_entry_naming_no_repo_is_unlinked(tmp_path: Path, registry, spawned) -> None:
    _register(registry, tmp_path / "gone" / ".git")
    _register(registry, tmp_path / "bare.git", name="b")
    assert mt.on_idle_tick([]) is False
    assert list(registry.iterdir()) == []


def test_registry_entry_under_the_system_temp_dir_is_unlinked(tmp_path: Path, registry, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "system-temp" / "x"
    (scratch / ".git").mkdir(parents=True)
    real = tmp_path / "real"
    (real / ".git").mkdir(parents=True)
    _register(registry, scratch / ".git", name="t")
    _register(registry, real / ".git", name="r")
    assert mt._read_store_repos() == [real]
    assert [p.name for p in registry.iterdir()] == ["r"]


def test_registry_is_capped_keeping_the_newest_entries(tmp_path: Path, registry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mt, "READ_STORE_REGISTRY_MAX", 2)
    now = time.time()
    for i in range(4):
        repo = tmp_path / f"r{i}"
        (repo / ".git").mkdir(parents=True)
        _register(registry, repo / ".git", name=f"e{i}")
        os.utime(registry / f"e{i}", (now - 100 + i, now - 100 + i))
    assert mt._read_store_repos() == [tmp_path / "r3", tmp_path / "r2"]
    assert sorted(p.name for p in registry.iterdir()) == ["e2", "e3"]


def test_unresolvable_settings_home_is_logged_not_raised(repo, spawned, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    from coordinator_core.git import git_objects

    def boom():
        raise ValueError("COORDINATOR_SETTINGS_HOME must be absolute")

    monkeypatch.setattr(git_objects, "read_store_registry_dir", boom)
    assert mt.on_idle_tick([repo]) is True
    assert "settings_home" in caplog.text


def _pause(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.git import pack_deletion_pause

    monkeypatch.setattr(pack_deletion_pause, "PACK_DELETION_PAUSED", True)


def test_paused_tick_offers_daily_only_when_midx_upkeep_is_owed(repo, spawned, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.git import store_hygiene

    _pause(monkeypatch)
    common = repo / ".git"
    _age(common, "daily", mt.DAILY_INTERVAL_S + 60)
    _age(common, "weekly", mt.WEEKLY_INTERVAL_S + 60)
    monkeypatch.setattr(store_hygiene, "midx_upkeep_owed", lambda c: True)
    assert mt.on_idle_tick([repo]) is True
    assert spawned == [(repo.resolve(), ["daily"])]


def test_paused_tick_with_a_current_midx_spawns_nothing(repo, spawned, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.git import store_hygiene

    _pause(monkeypatch)
    monkeypatch.setattr(store_hygiene, "midx_upkeep_owed", lambda c: False)
    assert mt.on_idle_tick([repo]) is False
    assert spawned == []


def _store(common: Path, packs: int, midx_packs: int) -> None:
    pack_dir = common / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    for i in range(packs):
        (pack_dir / f"pack-{i:040x}.idx").write_bytes(b"")
    (pack_dir / "multi-pack-index").write_bytes(b"MIDX" + bytes([1, 1, 0, 0]) + midx_packs.to_bytes(4, "big"))


def _future_stamp(common: Path, tier: str) -> None:
    _age(common, tier, -30 * 24 * 3600)


def test_paused_tick_fires_for_a_midx_missing_one_pack_despite_a_future_dated_stamp(
    repo, spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pause(monkeypatch)
    common = repo / ".git"
    _store(common, packs=5, midx_packs=4)
    _future_stamp(common, "daily")
    assert mt.on_idle_tick([repo]) is True
    assert spawned == [(repo.resolve(), ["daily"])]
    assert time.time() - mt.stamp_path(common, "daily").stat().st_mtime < 60


def test_paused_tick_with_a_midx_covering_every_pack_spawns_nothing(
    repo, spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pause(monkeypatch)
    common = repo / ".git"
    _store(common, packs=5, midx_packs=5)
    _future_stamp(common, "daily")
    assert mt.on_idle_tick([repo]) is False
    assert spawned == []


def test_a_future_dated_stamp_is_owed_not_a_claim(repo) -> None:
    common = repo / ".git"
    _future_stamp(common, "daily")
    assert mt._owed(common, "daily", time.time()) is True
    assert mt._threshold_cooled(common, time.time()) is True


def test_paused_child_runs_the_daily_tier_light(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.git import fleet_maintenance_lock, store_hygiene
    from coordinator_core.ops import git_maintenance

    _pause(monkeypatch)
    common = repo / ".git"
    _store(common, packs=5, midx_packs=4)
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(store_hygiene, "_box_busy", lambda: False)
    monkeypatch.setattr(
        git_maintenance,
        "run_tier",
        lambda r, tier, heavy=True: seen.append((tier, heavy)) or SimpleNamespace(rc=0, deferred=False, errors=[]),
    )
    monkeypatch.setattr(fleet_maintenance_lock, "hold", lambda **kw: __import__("contextlib").nullcontext())
    assert mt.run_due(repo, ["daily"]) == 0
    assert seen == [("daily", False)]


def test_child_backdates_a_deferred_weekly(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_child(monkeypatch, busy=False, deferred=True)
    common = repo / ".git"
    _age(common, "weekly", 0)
    mt.run_due(repo, ["weekly"])
    age = time.time() - mt.stamp_path(common, "weekly").stat().st_mtime
    assert age > mt.WEEKLY_INTERVAL_S - mt.RETRY_S - 60


def test_child_backdates_a_deferred_daily(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_child(monkeypatch, busy=False, daily=(0, True))
    common = repo / ".git"
    _age(common, "daily", 0)
    mt.run_due(repo, ["daily"])
    age = time.time() - mt.stamp_path(common, "daily").stat().st_mtime
    assert age > mt.DAILY_INTERVAL_S - mt.RETRY_S - 60


def test_child_keeps_the_stamp_of_a_completed_daily(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_child(monkeypatch, busy=False, daily=(0, False))
    common = repo / ".git"
    _age(common, "daily", 0)
    mt.run_due(repo, ["daily"])
    assert time.time() - mt.stamp_path(common, "daily").stat().st_mtime < 60


def test_child_backdates_a_failed_weekly(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_child(monkeypatch, busy=False, weekly_rc=1)
    common = repo / ".git"
    _age(common, "weekly", 0)
    assert mt.run_due(repo, ["weekly"]) == 1
    age = time.time() - mt.stamp_path(common, "weekly").stat().st_mtime
    assert age > mt.WEEKLY_INTERVAL_S - mt.RETRY_S - 60


def test_no_op_tick_starts_no_process(repo, spawned, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    common = repo / ".git"
    _age(common, "daily", 60)
    _age(common, "weekly", 60)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("spawned a process"))
    assert mt.on_idle_tick([repo]) is False
    assert spawned == []


@pytest.mark.parametrize("tier", ["daily", "weekly"])
def test_backdated_tier_is_owed_after_retry_delay_and_not_before(repo, tier) -> None:
    common = repo / ".git"
    mt._backdate(common, tier)
    now = time.time()
    assert not mt._owed(common, tier, now + mt.RETRY_S - 120)
    assert mt._owed(common, tier, now + mt.RETRY_S + 120)


def test_main_rejects_bad_usage() -> None:
    assert mt.main(["--bogus"]) == 2
