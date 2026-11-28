"""prune_claude_temp: a live session's dir survives, and the scratch budget never takes in-flight work."""

from __future__ import annotations

import os
import time

from coordinator_core import temp_layout
from coordinator_core.ops import prune_claude_temp as pct
from pathlib import Path
import pytest

HOUR = 3600


def _write(path, size, age_s, now) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (now - age_s, now - age_s))


def _claude_root(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    root.mkdir()
    monkeypatch.setattr(pct, "claude_temp_root", lambda: root)
    monkeypatch.setattr(pct, "_claude_retention_days", lambda: 2)
    monkeypatch.setattr(pct, "scratch_budget_bytes", lambda get=None: 10 ** 12)
    return root


def test_live_session_dir_kept_past_retention_dead_one_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _claude_root(tmp_path, monkeypatch)
    now = time.time()
    _write(root / "proj" / "live" / "scratchpad" / "work.txt", 1, 5 * 24 * HOUR, now)
    _write(root / "proj" / "live" / "scratchpad" / "fresh.txt", 1, HOUR, now)
    _write(root / "proj" / "dead" / "scratchpad" / "work.txt", 1, 5 * 24 * HOUR, now)
    old = (now - 5 * 24 * HOUR,) * 2
    for d in ("live", "dead"):
        os.utime(root / "proj" / d / "scratchpad", old)
        os.utime(root / "proj" / d, old)
    os.utime(root / "proj" / "live" / "scratchpad", (now, now))
    pct.prune(roots=[(root, 2, 2)], now=now, drain=True, live_session_ids=frozenset({"live"}))
    assert (root / "proj" / "live").is_dir()
    assert (root / "proj" / "live" / "scratchpad" / "fresh.txt").exists()
    assert not (root / "proj" / "dead").exists()


def test_unknown_liveness_keeps_every_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _claude_root(tmp_path, monkeypatch)
    now = time.time()
    _write(root / "proj" / "s1" / "scratchpad" / "work.txt", 1, 5 * 24 * HOUR, now)
    pct.prune(roots=[(root, 2, 2)], now=now, drain=True, live_session_ids=None)
    assert (root / "proj" / "s1" / "scratchpad" / "work.txt").exists()


def test_budget_deletes_oldest_idle_until_session_fits(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    now = time.time()
    pad = root / "proj" / "s1" / "scratchpad"
    _write(pad / "oldest.bin", 100, 10 * HOUR, now)
    _write(pad / "older.bin", 100, 5 * HOUR, now)
    _write(pad / "idle.bin", 100, 2 * HOUR, now)
    removed = pct._enforce_scratch_budget(root, 150, now, pct._Budget(None))
    assert removed == 2
    assert sorted(p.name for p in pad.iterdir()) == ["idle.bin"]


def test_tree_stats_of_a_loose_file_is_its_own_size_and_mtime(tmp_path: Path) -> None:
    now = time.time()
    f = tmp_path / "loose.bin"
    _write(f, 123, 600, now)
    size, newest = pct._tree_stats(str(f), pct._Budget(None))
    assert size == 123
    assert abs(newest - (now - 600)) < 2


def test_loose_files_count_toward_the_budget(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    now = time.time()
    pad = root / "proj" / "s1" / "scratchpad"
    _write(pad / "a.bin", 100, 10 * HOUR, now)
    _write(pad / "b.bin", 100, 9 * HOUR, now)
    assert pct.over_budget_sessions(root, 150, pct._Budget(None)) == {str(root / "proj" / "s1"): 200}


def test_over_budget_session_keeps_fresh_loose_file_beside_old_large_dir(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    now = time.time()
    pad = root / "proj" / "s1" / "scratchpad"
    _write(pad / "bigdir" / "blob.bin", 1000, 10 * HOUR, now)
    _write(pad / "fresh.txt", 10, 5, now)
    removed = pct._enforce_scratch_budget(root, 100, now, pct._Budget(None))
    assert removed == 1
    assert sorted(p.name for p in pad.iterdir()) == ["fresh.txt"]


def test_over_budget_session_keeps_a_fresh_empty_dir(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    now = time.time()
    pad = root / "proj" / "s1" / "scratchpad"
    _write(pad / "old.bin", 1000, 10 * HOUR, now)
    (pad / "fresh_dir").mkdir()
    removed = pct._enforce_scratch_budget(root, 100, now, pct._Budget(None))
    assert removed == 1
    assert sorted(p.name for p in pad.iterdir()) == ["fresh_dir"]


def test_prune_reaps_dead_pytest_runs_inside_the_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import time

    from coordinator_core.group_em import session_registry
    from coordinator_core.temp_layout import PYTEST_KEPT_MARKER

    monkeypatch.setattr(pct.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(temp_layout.tempfile, "gettempdir", lambda: str(tmp_path))
    dead = 3_900_000
    while session_registry.pid_alive(dead) and dead > 3_000_000:
        dead -= 17
    base = tmp_path / "coordinator" / "myrepo" / "pytest"
    dead_dir = base / f"{dead}-abc123"
    dead_dir.mkdir(parents=True)
    (dead_dir / "nested" / "f.txt").parent.mkdir()
    (dead_dir / "nested" / "f.txt").write_text("x")
    own = base / f"{os.getpid()}-abc123"
    own.mkdir()
    kept = base / f"{dead}-def456"
    kept.mkdir()
    (kept / PYTEST_KEPT_MARKER).write_text("")
    odd = base / "not-a-run"
    odd.mkdir()
    fleet = tmp_path / "coordinator" / "_fleet" / "pytest" / f"{dead}-abc123"
    fleet.mkdir(parents=True)
    started = time.process_time()
    pct.prune(repo_root=str(tmp_path / "myrepo"))
    assert time.process_time() - started < pct.BUDGET_SECONDS
    assert not dead_dir.exists()
    assert own.exists() and kept.exists() and odd.exists() and fleet.exists()
    assert (tmp_path / "coordinator" / pct.STAMP_NAME).is_file()


def test_prune_pytest_backlog_holds_the_stamp_then_a_later_pass_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordinator_core.group_em import session_registry

    monkeypatch.setattr(pct.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(temp_layout.tempfile, "gettempdir", lambda: str(tmp_path))
    dead = 3_800_000
    while session_registry.pid_alive(dead) and dead > 3_000_000:
        dead -= 17
    run = tmp_path / "coordinator" / "myrepo" / "pytest" / f"{dead}-abc123"
    run.mkdir(parents=True)
    (run / "f.txt").write_text("x")
    pct.prune(budget_seconds=0)
    assert run.exists()
    assert not (tmp_path / "coordinator" / pct.STAMP_NAME).exists()
    pct.prune()
    assert not run.exists()
    assert (tmp_path / "coordinator" / pct.STAMP_NAME).is_file()


def test_budget_never_takes_a_child_written_within_the_idle_window(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    now = time.time()
    pad = root / "proj" / "s1" / "scratchpad"
    _write(pad / "old.bin", 100, 10 * HOUR, now)
    _write(pad / "in_flight.bin", 500, 60, now)
    removed = pct._enforce_scratch_budget(root, 150, now, pct._Budget(None))
    assert removed == 1
    assert sorted(p.name for p in pad.iterdir()) == ["in_flight.bin"]


LIVE_PID = 4242
DEAD_PIDS = (90000001, 90000002, 90000003, 90000004, 90000005, 90000006)


class _CountedBudget(pct._Budget):
    """Latches after a fixed number of checks, so a pass is deterministic whatever the process-time clock."""

    def __init__(self, limit=pct.BUDGET_SECONDS) -> None:
        super().__init__(limit)
        self.n = 0

    def check(self) -> bool:
        self.n += 1
        if self.limit is not None and self.n >= 25:
            self.stopped = True
        return self.stopped


def _run_dir(pdir: Path, pid: int, files: int = 6) -> Path:
    d = pdir / f"{pid}-abc123"
    for i in range(files):
        f = d / "sub" / f"f{i}.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    return d


def _pytest_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from coordinator_core.group_em import session_registry
    from coordinator_core.temp_layout import PYTEST_KEPT_MARKER

    base = tmp_path / "coordinator"
    pdir = base / "repoA" / "pytest"
    monkeypatch.setattr(session_registry, "pid_alive", lambda pid: pid == LIVE_PID)
    monkeypatch.setattr(pct, "fleet_temp_root", lambda: base / "_fleet")
    monkeypatch.setattr(pct, "default_roots", lambda repo_root="": [])
    monkeypatch.setattr(pct, "_prune_top_level", lambda cutoff, budget=None, keep=frozenset(): 0)
    monkeypatch.setattr(pct, "_Budget", _CountedBudget)
    dead = [_run_dir(pdir, p) for p in DEAD_PIDS]
    survivors = {
        "live": _run_dir(pdir, LIVE_PID),
        "own": _run_dir(pdir, os.getpid()),
        "kept": _run_dir(pdir, 90000100),
        "bad": pdir / "not-a-run",
        "fleet": _run_dir(base / "_fleet" / "pytest", 90000200),
    }
    survivors["bad"].mkdir()
    (survivors["kept"] / PYTEST_KEPT_MARKER).write_text("")
    return base, pdir, dead, survivors


def test_pytest_leg_drains_dead_runs_across_passes_without_a_recursive_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    base, pdir, dead, survivors = _pytest_fixture(tmp_path, monkeypatch)
    now = time.time()

    def _refuse(*_a, **_k):
        raise AssertionError("recursive walk on the prune path")

    stamp = base / pct.STAMP_NAME
    passes = 0
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "walk", _refuse)
        scoped.setattr(shutil, "rmtree", _refuse)
        while not stamp.exists():
            passes += 1
            assert passes < 200
            pct.prune(now=now, pytest_roots=[base])
    assert passes > 1
    assert not any(d.exists() for d in dead)
    assert not (pdir / ".reap").exists() or not list((pdir / ".reap").iterdir())
    assert all(p.exists() for p in survivors.values())


def test_stamp_is_withheld_while_pytest_backlog_remains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base, pdir, dead, _ = _pytest_fixture(tmp_path, monkeypatch)
    pct.prune(now=time.time(), pytest_roots=[base], budget_seconds=0.001)
    assert not (base / pct.STAMP_NAME).exists()
    assert pct.roots_due(pytest_roots=[base])


def test_a_latched_main_budget_does_not_block_the_top_level_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, pdir, dead, _ = _pytest_fixture(tmp_path, monkeypatch)
    for d in dead:
        for f in sorted(d.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        d.rmdir()
    monkeypatch.setattr(pct, "default_roots", lambda repo_root="": [(base / "_fleet", 1, 7)])

    def _latch(*a, **_k):
        a[6].stopped = True
        return 0

    monkeypatch.setattr(pct, "_prune_level", _latch)
    pct.prune(now=time.time(), pytest_roots=[base])
    assert (base / pct.STAMP_NAME).exists()


def test_roots_due_only_for_a_dead_unmarked_run_or_a_nonempty_reap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, pdir, dead, survivors = _pytest_fixture(tmp_path, monkeypatch)
    base.mkdir(exist_ok=True)
    (base / pct.STAMP_NAME).touch()
    assert pct.roots_due(pytest_roots=[base])
    for d in dead:
        target = pdir / ".reap" / d.name
        target.parent.mkdir(exist_ok=True)
        d.rename(target)
    assert pct.roots_due(pytest_roots=[base])
    for f in sorted((pdir / ".reap").rglob("*"), reverse=True):
        f.unlink() if f.is_file() else f.rmdir()
    assert not pct.roots_due(pytest_roots=[base])


def test_an_undeletable_reap_entry_stops_being_due_once_a_pass_has_stamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, pdir, dead, _ = _pytest_fixture(tmp_path, monkeypatch)
    for d in dead:
        for f in sorted(d.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        d.rmdir()
    stuck = pdir / ".reap" / "stuck"
    stuck.mkdir(parents=True)
    old = time.time() - 120
    os.utime(pdir / ".reap", (old, old))
    base.mkdir(exist_ok=True)
    (base / pct.STAMP_NAME).touch()
    assert not pct.roots_due(pytest_roots=[base])
    os.utime(pdir / ".reap", None)
    assert pct.roots_due(pytest_roots=[base])


def test_backlog_counts_a_dead_run_whose_kept_marker_expired_not_a_fresh_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordinator_core.temp_layout import PYTEST_KEPT_MARKER, _KEPT_MAX_AGE_S

    base, pdir, dead, survivors = _pytest_fixture(tmp_path, monkeypatch)
    for d in dead:
        for f in sorted(d.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        d.rmdir()
    now = time.time()
    assert not pct._pytest_backlog([base], now)
    marker = survivors["kept"] / PYTEST_KEPT_MARKER
    old = now - _KEPT_MAX_AGE_S - 60
    os.utime(marker, (old, old))
    assert pct._pytest_backlog([base], now)


def test_reap_dirname_matches_the_sweep() -> None:
    from coordinator_core.ops.fleet import pytest_basetemp_sweep as sweep

    assert pct._REAP_DIRNAME == sweep.REAP_DIRNAME


def test_tombstoning_takes_half_the_pytest_budget_then_the_leg_reads_as_undrained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordinator_core.ops.fleet import pytest_basetemp_sweep as sweep

    clock = iter(x * 0.01 for x in range(10_000))
    monkeypatch.setattr(pct.time, "process_time", lambda: next(clock))
    ticks = []
    drained = []

    def tombstone(*, roots, now, check):
        while not check():
            ticks.append(1)
        return len(ticks)

    monkeypatch.setattr(sweep, "tombstone_dead_runs", tombstone)
    monkeypatch.setattr(sweep, "drain_tombstones", lambda **_k: drained.append(1))
    budget = pct._Budget(0.12)
    pct._run_pytest_leg(budget, None, 0.0)
    assert 0 < len(ticks) < 12
    assert budget.stopped
    assert drained == [1]
