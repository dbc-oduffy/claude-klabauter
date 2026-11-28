"""Tests for the pytest basetemp sweeper; every root is a tmp_path, never the real temp dir."""
from __future__ import annotations

import getpass
import os
import threading

from coordinator_core.temp_layout import PYTEST_KEPT_MARKER
from coordinator_core.warm import pytest_basetemp_sweep as sw

NOW = 1_000_000_000.0
OLD = NOW - 7 * 3600
YOUNG = NOW - 3600
DAY = 86400.0
DEAD_PID = 2_000_000_000


def _mk(tmp_path, name, mtime, lock=None, files=3):
    root = tmp_path / f"pytest-of-{getpass.getuser()}"
    d = root / name
    (d / "sub").mkdir(parents=True)
    for i in range(files):
        (d / "sub" / f"f{i}").write_text("x")
    if lock is not None:
        (d / ".lock").write_text(str(lock))
    os.utime(d, (mtime, mtime))
    return d


def _sweep(tmp_path, alive=lambda pid: False, **kw):
    sleeps = []
    report = sw.sweep_once(
        temp_root=str(tmp_path), now=NOW, pid_alive=alive, sleep=sleeps.append, **kw
    )
    return report, sleeps


def test_old_dead_lock_removed_and_young_kept(tmp_path):
    old = _mk(tmp_path, "pytest-1", OLD, lock=111)
    young = _mk(tmp_path, "pytest-2", YOUNG, lock=222)
    report, _ = _sweep(tmp_path)
    assert not old.exists()
    assert young.exists()
    assert report["removed"] == ["pytest-1"]
    assert report["kept"] == 1


def test_old_live_pid_dir_is_kept(tmp_path):
    live = _mk(tmp_path, "pytest-3", OLD, lock=333)
    dead = _mk(tmp_path, "pytest-4", OLD, lock=444)
    _sweep(tmp_path, alive=lambda pid: pid == 333)
    assert live.exists()
    assert not dead.exists()


def test_old_dir_without_lock_is_removed(tmp_path):
    d = _mk(tmp_path, "pytest-5", OLD)
    _sweep(tmp_path)
    assert not d.exists()


def test_non_basetemp_names_are_untouched(tmp_path):
    other = _mk(tmp_path, "keep-me", OLD)
    current = _mk(tmp_path, "pytest-current", OLD)
    _sweep(tmp_path)
    assert other.exists() and current.exists()


def test_deletes_pause_between_batches(tmp_path):
    _mk(tmp_path, "pytest-6", OLD, files=1)
    d = tmp_path / f"pytest-of-{getpass.getuser()}" / "pytest-6"
    for i in range(5):
        (d / f"extra{i}").write_text("x")
    os.utime(d, (OLD, OLD))  # creating children bumped mtime; restore age
    _, sleeps = _sweep(tmp_path, batch_size=2)
    assert not d.exists()
    assert len(sleeps) >= 3


def test_missing_root_is_a_quiet_noop(tmp_path):
    report, _ = _sweep(tmp_path)
    assert report == {"removed": [], "kept": 0, "failed": []}


def test_failure_is_caught_and_reported(tmp_path, monkeypatch):
    d = _mk(tmp_path, "pytest-7", OLD)

    def boom(*a, **k):
        raise OSError("locked")

    monkeypatch.setattr(sw, "_rmtree_bounded", boom)
    report, _ = _sweep(tmp_path)
    assert report["failed"] == ["pytest-7"]
    assert d.exists()


def test_loop_sweeps_at_start_then_waits_interval(tmp_path, monkeypatch):
    calls = []
    stop = threading.Event()

    def fake_sweep(**kw):
        calls.append(kw)
        stop.set()
        return {"removed": [], "kept": 0, "failed": []}

    monkeypatch.setattr(sw, "sweep_once", fake_sweep)
    sw.sweep_loop(stop=stop, interval_secs=3600, temp_root=str(tmp_path))
    assert len(calls) == 1


# --- coordinator-root sweep: the writers measured into <temp>/coordinator ---


def _touch(path, mtime, *, is_dir=False):
    if is_dir:
        (path / "sub").mkdir(parents=True)
        (path / "sub" / "f").write_text("x")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    os.utime(path, (mtime, mtime))
    return path


def _coord(tmp_path, alive=lambda pid: False, **kw):
    return sw.sweep_coordinator_roots(
        temp_root=str(tmp_path), now=NOW, pid_alive=alive, sleep=lambda s: None, **kw
    )


def test_fleet_session_files_older_than_a_week_are_reaped(tmp_path):
    fleet = tmp_path / "coordinator" / "_fleet"
    old = _touch(fleet / "workflow-stopped-abc", NOW - 8 * DAY)
    old_hook = _touch(fleet / "advisory-hook-state-s1.json", NOW - 8 * DAY)
    young = _touch(fleet / "workflow-stopped-def", NOW - 1 * DAY)
    foreign = _touch(fleet / "somebody-elses-file", NOW - 30 * DAY)
    report = _coord(tmp_path)
    assert not old.exists() and not old_hook.exists()
    assert young.exists() and foreign.exists()
    assert len(report["removed"]) == 2


def test_publish_engine_keeps_newest_two_and_drops_old(tmp_path):
    engine = tmp_path / "coordinator" / "_fleet" / "claude-klabauter-publish-engine"
    shas = [f"{i:040x}" for i in range(12)]
    for i, sha in enumerate(shas):  # i=0 newest, all older than the surplus grace
        _touch(engine / sha, NOW - 3 * 3600 - i * 60, is_dir=True)
    ancient = _touch(engine / ("f" * 40), NOW - 3 * DAY, is_dir=True)
    _coord(tmp_path)
    kept = sorted(p.name for p in engine.iterdir())
    assert kept == sorted(shas[:2])
    assert not ancient.exists()


def test_every_keep_latest_writer_holds_two_after_one_sweep(tmp_path):
    from coordinator_core.temp_layout import COORDINATOR_ROOT_WRITERS, SCOPE_FLEET

    fleet = tmp_path / "coordinator" / "_fleet"
    repo = tmp_path / "coordinator" / "somerepo"
    holders = {}
    for w in COORDINATOR_ROOT_WRITERS:
        if w.keep_latest is None or w.owner == "lease":
            continue
        parent = w.directory(fleet if w.scope == SCOPE_FLEET else repo)
        stem = w.prefixes[0] if w.prefixes else "m-"
        for i in range(5):
            _touch(parent / f"{stem}{i}", NOW - 3 * 3600 - i * 60, is_dir=True)
        holders[w.name] = (parent, w)
    _coord(tmp_path)
    assert holders
    for name, (parent, w) in holders.items():
        left = [p for p in parent.iterdir() if w.matches(p.name)]
        assert len(left) == w.keep_latest, name


def test_size_bound_evicts_dead_owned_oldest_first_and_spares_live_and_kept(tmp_path):
    pytest_dir = tmp_path / "coordinator" / "repoA" / "pytest"
    big = b"x" * 1000
    live = _touch(pytest_dir / "4242-bbbbbb", NOW - 5 * DAY, is_dir=True)
    kept = _touch(pytest_dir / "kept-1", NOW - 2 * DAY, is_dir=True)
    (kept / PYTEST_KEPT_MARKER).write_text("")
    os.utime(kept, (NOW - 2 * DAY, NOW - 2 * DAY))
    older = _touch(pytest_dir / "warp-older", NOW - 1800, is_dir=True)
    newer = _touch(pytest_dir / "warp-newer", NOW - 600, is_dir=True)
    for d in (live, kept, older, newer):
        (d / "sub" / "blob").write_bytes(big)
    _coord(tmp_path, alive=lambda pid: pid == 4242, size_budget_bytes=3100)
    assert live.exists() and kept.exists()
    assert not older.exists() and newer.exists()


def test_size_bound_unset_budget_leaves_survivors_when_under(tmp_path):
    d = _touch(tmp_path / "coordinator" / "repoA" / "pytest" / "warp-x", NOW - 60, is_dir=True)
    _coord(tmp_path, size_budget_bytes=10**9)
    assert d.exists()


def test_size_walk_runs_once_per_survivor(tmp_path, monkeypatch):
    mirror = tmp_path / "coordinator" / "somerepo" / "clean-mirror"
    for i in range(2):
        _touch(mirror / f"m{i}", NOW - 3600, is_dir=True)
    calls = []
    real = sw._tree_bytes
    monkeypatch.setattr(sw, "_tree_bytes", lambda p, e: calls.append(p) or real(p, e))
    _coord(tmp_path, size_budget_bytes=1)
    assert len(calls) == len(set(calls)) == 2


def test_coordinator_root_budget_reads_the_settings_key():
    assert sw.coordinator_root_budget_bytes(lambda k: "3") == 3 * sw.GIB
    assert sw.coordinator_root_budget_bytes(lambda k: None) == sw.DEFAULT_COORDINATOR_ROOT_GIB * sw.GIB
    assert sw.KEY_COORDINATOR_ROOT_GIB == "temp_budget.coordinator_root_gib"


def test_publish_engine_surplus_inside_grace_is_kept(tmp_path):
    engine = tmp_path / "coordinator" / "_fleet" / "claude-klabauter-publish-engine"
    for i in range(10):
        _touch(engine / f"{i:040x}", NOW - 60 - i, is_dir=True)
    _coord(tmp_path)
    assert len(list(engine.iterdir())) == 10


def test_fleet_cache_dirs_age_out_after_a_day(tmp_path):
    fleet = tmp_path / "coordinator" / "_fleet"
    old_inject = _touch(fleet / "claude-klabauter-publish-inject-ab", NOW - 2 * DAY, is_dir=True)
    old_tmp = _touch(fleet / "tmpabcd1234", NOW - 2 * DAY, is_dir=True)
    young_inject = _touch(fleet / "claude-klabauter-publish-inject-cd", NOW - 3600, is_dir=True)
    unrelated = _touch(fleet / "tmp-not-a-mkdtemp", NOW - 9 * DAY, is_dir=True)
    _coord(tmp_path)
    assert not old_inject.exists() and not old_tmp.exists()
    assert young_inject.exists() and unrelated.exists()


def test_clone_keyed_pytest_basetemps_follow_pid_and_age_rules(tmp_path):
    pytest_dir = tmp_path / "coordinator" / "deadbeefclone" / "pytest"
    dead = _touch(pytest_dir / f"{DEAD_PID}-aaaaaa", NOW - 2 * 3600, is_dir=True)
    live = _touch(pytest_dir / "4242-bbbbbb", NOW - 5 * DAY, is_dir=True)
    fresh_dead = _touch(pytest_dir / f"{DEAD_PID}-cccccc", NOW - 60, is_dir=True)
    named_old = _touch(pytest_dir / "ask-explicit", NOW - 2 * DAY, is_dir=True)
    named_young = _touch(pytest_dir / "warp-explicit", NOW - 3600, is_dir=True)
    kept_young = _touch(pytest_dir / "kept-1", NOW - 2 * DAY, is_dir=True)
    (kept_young / PYTEST_KEPT_MARKER).write_text("")
    os.utime(kept_young, (NOW - 2 * DAY, NOW - 2 * DAY))
    kept_old = _touch(pytest_dir / "kept-2", NOW - 8 * DAY, is_dir=True)
    (kept_old / PYTEST_KEPT_MARKER).write_text("")
    os.utime(kept_old, (NOW - 8 * DAY, NOW - 8 * DAY))
    _coord(tmp_path, alive=lambda pid: pid == 4242)
    assert not dead.exists() and not named_old.exists() and not kept_old.exists()
    assert live.exists() and fresh_dead.exists()
    assert named_young.exists() and kept_young.exists()


def test_clean_mirror_entries_age_out_per_repo(tmp_path):
    mirror = tmp_path / "coordinator" / "somerepo" / "clean-mirror"
    old = _touch(mirror / "m1", NOW - 2 * DAY, is_dir=True)
    young = _touch(mirror / "m2", NOW - 3600, is_dir=True)
    _coord(tmp_path)
    assert not old.exists() and young.exists()


def test_dead_owner_run_temp_is_reaped_for_every_clone(tmp_path):
    run_temp = tmp_path / "coordinator" / "cloneXYZ" / "run-temp"
    dead = _touch(run_temp / f"{DEAD_PID}-abcdef-x", NOW, is_dir=True)
    _coord(tmp_path)
    assert not dead.exists()


def test_tick_budget_stops_and_resumes_next_tick(tmp_path):
    fleet = tmp_path / "coordinator" / "_fleet"
    for i in range(5):
        _touch(fleet / f"workflow-stopped-{i}", NOW - 9 * DAY)
    spent = sw.sweep_coordinator_roots(
        temp_root=str(tmp_path), now=NOW, pid_alive=lambda p: False,
        sleep=lambda s: None, budget_secs=-1.0,
    )
    assert spent["removed"] == [] and len(list(fleet.iterdir())) == 5
    _coord(tmp_path)
    assert list(fleet.iterdir()) == []


def test_loop_also_sweeps_the_coordinator_root(tmp_path):
    old = _touch(tmp_path / "coordinator" / "_fleet" / "workflow-stopped-z", 1.0)
    stop = threading.Event()
    stop.wait = lambda *_a, **_k: stop.set()
    sw.sweep_loop(stop=stop, temp_root=str(tmp_path), sleep=lambda s: None, pid_alive=lambda p: False)
    assert not old.exists()


def test_missing_coordinator_root_is_a_quiet_noop(tmp_path):
    assert _coord(tmp_path) == {"removed": [], "failed": []}


def test_expiry_inside_one_big_tree_stops_midway_keeps_age_and_resumes(tmp_path):
    tree = tmp_path / "coordinator" / "cloneBIG" / "clean-mirror" / "m1"
    for d in range(4):
        for f in range(5):
            (tree / f"d{d}" / f"f{f}").parent.mkdir(parents=True, exist_ok=True)
            (tree / f"d{d}" / f"f{f}").write_text("x")
    os.utime(tree, (NOW - 2 * DAY, NOW - 2 * DAY))
    calls = {"n": 0}

    def expired() -> bool:
        calls["n"] += 1
        return calls["n"] > 6

    sw._remove_in_batches(tree, batch_size=8, pause_secs=0, sleep=lambda s: None, expired=expired)
    left = sum(1 for _ in tree.rglob("*"))
    assert tree.exists() and 0 < left < 24
    assert abs(tree.stat().st_mtime - (NOW - 2 * DAY)) < 2
    _coord(tmp_path)
    assert not tree.exists()
