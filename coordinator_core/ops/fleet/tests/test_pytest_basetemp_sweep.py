"""Guard tests G5-G8 for the cross-repo pytest basetemp sweep."""

from __future__ import annotations

import json
import os
import tempfile
import time

import pytest

from coordinator_core.group_em import session_registry
from coordinator_core.ops.fleet import pytest_basetemp_sweep as sweep
from coordinator_core.temp_layout import FLEET_DIRNAME, PYTEST_KEPT_MARKER
from pathlib import Path

_OLD = time.time() - 10 * 3600.0


@pytest.fixture
def temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "Temp"
    base.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(base))
    monkeypatch.setattr(sweep, "pytest_sweep_roots", lambda: [base / "coordinator"])
    return base / "coordinator"


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    d = tmp_path / "registry"
    d.mkdir()
    return d


@pytest.fixture
def dead_pid() -> int:
    """A pid the OS reports dead, probed without spawning (the spawn ratchet admits no new spawning tests)."""
    for pid in range(4_000_000, 3_000_000, -7):
        if not session_registry.pid_alive(pid):
            return pid
    raise AssertionError("no dead pid found")


def _mk(path, files=("a.txt",)):
    path.mkdir(parents=True)
    for name in files:
        (path / name).write_text("x")
    for p in [*path.rglob("*"), path]:
        os.utime(p, (_OLD, _OLD))
    return path


def test_g5_deletes_dead_pid_dirs_across_repos(temp, registry, dead_pid) -> None:
    a = _mk(temp / "repo-a" / "pytest" / f"{dead_pid}-a1b2c3")
    b = _mk(temp / "repo-b" / "pytest" / f"{dead_pid}-d4e5f6")
    recs = sweep.sweep_dead_runs(registry_dir=registry)
    assert not a.exists() and not b.exists()
    assert sorted(r["action"] for r in recs) == ["deleted", "deleted"]


def test_g6_never_deletes_live_or_own_pid(temp, registry, dead_pid, monkeypatch: pytest.MonkeyPatch) -> None:
    # A real foreign pid cannot be probed here: the conftest os.kill tripwire refuses
    # signalling outside the test's process tree, so liveness of "another" pid is stubbed.
    live_pid = dead_pid - 1
    monkeypatch.setattr(session_registry, "pid_alive", lambda pid: pid == live_pid)
    own = _mk(temp / "repo" / "pytest" / f"{os.getpid()}-a1b2c3")
    live = _mk(temp / "repo" / "pytest" / f"{live_pid}-a1b2c3")
    dead = _mk(temp / "repo" / "pytest" / f"{dead_pid}-a1b2c3")
    sweep.sweep_dead_runs(registry_dir=registry)
    assert not dead.exists()
    assert own.exists() and live.exists()


def test_g7_leaves_marked_unparsable_and_fleet(temp, registry, dead_pid) -> None:
    marked = _mk(temp / "repo" / "pytest" / f"{dead_pid}-a1b2c3", files=(PYTEST_KEPT_MARKER,))
    odd = _mk(temp / "repo" / "pytest" / "abc-a1b2c3")
    fleet = _mk(temp / FLEET_DIRNAME / "pytest" / f"{dead_pid}-a1b2c3")
    dead = _mk(temp / "repo" / "pytest" / f"{dead_pid}-d4e5f6")
    sweep.sweep_dead_runs(registry_dir=registry)
    assert not dead.exists()
    assert marked.exists() and odd.exists() and fleet.exists()


def test_g8_dry_run_deletes_nothing_and_prints_would_delete(temp, dead_pid, capsys: pytest.CaptureFixture[str]) -> None:
    run = _mk(temp / "repo" / "pytest" / f"{dead_pid}-a1b2c3")
    assert sweep.main(["--dry-run"]) == 0
    assert run.exists()
    recs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["action"] for r in recs] == ["would-delete"]


def _tree(run, dirs=3, files=4):
    for d in range(dirs):
        (run / f"d{d}").mkdir(parents=True)
        for f in range(files):
            (run / f"d{d}" / f"f{f}.txt").write_text("x")
    return run


def test_tombstone_renames_dead_runs_without_a_walk(temp, dead_pid, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    def boom(*_a, **_k):
        raise AssertionError("recursive walk on the rename leg")

    run = _tree(_mk(temp / "repo" / "pytest" / f"{dead_pid}-a1b2c3"))
    with monkeypatch.context() as m:
        m.setattr(os, "walk", boom)
        m.setattr(shutil, "rmtree", boom)
        assert sweep.tombstone_dead_runs() == 1
    assert not run.exists()
    assert (temp / "repo" / "pytest" / ".reap" / run.name / "d0" / "f0.txt").exists()


def test_tombstone_spares_live_own_marked_fresh_unparsable_and_fleet(temp, dead_pid, monkeypatch: pytest.MonkeyPatch) -> None:
    live_pid = dead_pid - 1
    monkeypatch.setattr(session_registry, "pid_alive", lambda pid: pid == live_pid)
    keep = [
        _mk(temp / "repo" / "pytest" / f"{os.getpid()}-a1b2c3"),
        _mk(temp / "repo" / "pytest" / f"{live_pid}-a1b2c3"),
        _mk(temp / "repo" / "pytest" / f"{dead_pid}-a1b2c3", files=(PYTEST_KEPT_MARKER,)),
        _mk(temp / "repo" / "pytest" / "abc-a1b2c3"),
        _mk(temp / FLEET_DIRNAME / "pytest" / f"{dead_pid}-a1b2c3"),
    ]
    assert sweep.tombstone_dead_runs() == 0
    assert all(p.exists() for p in keep)


def test_tombstone_takes_a_kept_dir_only_past_the_kept_age(temp, dead_pid) -> None:
    run = _mk(temp / "repo" / "pytest" / f"{dead_pid}-a1b2c3", files=(PYTEST_KEPT_MARKER,))
    marker = run / PYTEST_KEPT_MARKER
    now = time.time()
    os.utime(marker, (now - 6 * 86400, now - 6 * 86400))
    assert sweep.tombstone_dead_runs(now=now) == 0
    os.utime(marker, (now - 8 * 86400, now - 8 * 86400))
    assert sweep.tombstone_dead_runs(now=now) == 1
    assert not run.exists()


def test_tombstone_stops_when_check_latches(temp, dead_pid) -> None:
    for suffix in ("a1b2c3", "d4e5f6", "0a0b0c"):
        _mk(temp / "repo" / "pytest" / f"{dead_pid}-{suffix}")
    assert sweep.tombstone_dead_runs(check=lambda: True) == 1


def test_tombstone_checks_the_budget_on_a_live_run_too_and_stops_scanning(
    temp, dead_pid, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_pid = dead_pid - 1
    monkeypatch.setattr(session_registry, "pid_alive", lambda pid: pid == live_pid)
    for suffix in ("a1b2c3", "d4e5f6", "0a0b0c"):
        _mk(temp / "repo" / "pytest" / f"{live_pid}-{suffix}")
    dead = _mk(temp / "repo" / "pytest" / f"{dead_pid}-ffffff")
    calls = 0

    def latch() -> bool:
        nonlocal calls
        calls += 1
        return True

    assert sweep.tombstone_dead_runs(check=latch) == 0
    assert calls == 1 and dead.exists()
    assert sweep.tombstone_dead_runs() == 1 and not dead.exists()


def test_tombstone_ignores_the_reap_dir_itself(temp, dead_pid) -> None:
    _mk(temp / "repo" / "pytest" / ".reap" / f"{dead_pid}-a1b2c3")
    assert sweep.tombstone_dead_runs() == 0


def test_drain_removes_whole_trees(temp, dead_pid) -> None:
    tomb = _tree(_mk(temp / "repo" / "pytest" / ".reap" / f"{dead_pid}-a1b2c3"))
    assert sweep.drain_tombstones(check=lambda: False) == 1 + 3 * 4 + 3 + 1
    assert not tomb.exists()


def test_drain_stops_at_check_and_resumes(temp, dead_pid) -> None:
    tomb = _tree(_mk(temp / "repo" / "pytest" / ".reap" / f"{dead_pid}-a1b2c3"))
    total = sum(1 for _ in tomb.rglob("*")) + 1
    calls = 0

    def latch() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 5

    first = sweep.drain_tombstones(check=latch)
    assert first == 5 and tomb.exists()
    assert sweep.drain_tombstones(check=lambda: False) == total - first
    assert not tomb.exists()


def test_drain_never_follows_a_link_out_of_the_tree(temp, dead_pid, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    tomb = _mk(temp / "repo" / "pytest" / ".reap" / f"{dead_pid}-a1b2c3")
    try:
        os.symlink(outside, tomb / "lnk", target_is_directory=True)
    except OSError:
        pytest.skip("symlink not permitted")
    sweep.drain_tombstones(check=lambda: False)
    assert (outside / "keep.txt").exists() and not tomb.exists()


def test_drain_skips_run_dirs_that_are_not_tombstoned(temp, dead_pid) -> None:
    run = _mk(temp / "repo" / "pytest" / f"{dead_pid}-a1b2c3")
    assert sweep.drain_tombstones(check=lambda: False) == 0
    assert run.exists()


def test_sweep_reaps_a_dead_run_under_a_disk_fallback_root(temp, registry, dead_pid, monkeypatch) -> None:
    disk = temp.parent / "vartmp" / "coordinator"
    monkeypatch.setattr(sweep, "pytest_sweep_roots", lambda: [temp.parent / "gone", disk])
    run = _mk(disk / "repo" / "pytest" / f"{dead_pid}-a1b2c3")
    sweep.sweep_dead_runs(registry_dir=registry)
    assert not run.exists()


def test_tombstone_renames_without_a_walk(temp, dead_pid, monkeypatch) -> None:
    import shutil

    run = _mk(temp / "repo" / "pytest" / f"{dead_pid}-a1b2c3")
    monkeypatch.setattr(os, "walk", lambda *a, **k: (_ for _ in ()).throw(AssertionError("walk")))
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rmtree")))
    try:
        assert sweep.tombstone_dead_runs(roots=[temp]) == 1
        assert not run.exists()
        assert (temp / "repo" / "pytest" / ".reap" / run.name).is_dir()
    finally:
        monkeypatch.undo()


def test_expired_kept_marker_is_tombstoned_fresh_one_is_not(temp, dead_pid) -> None:
    from coordinator_core.temp_layout import PYTEST_KEPT_MARKER, _KEPT_MAX_AGE_S

    fresh = _mk(temp / "repo" / "pytest" / f"{dead_pid}-a1b2c3", files=(PYTEST_KEPT_MARKER,))
    stale = _mk(temp / "repo" / "pytest" / f"{dead_pid}-d4e5f6", files=(PYTEST_KEPT_MARKER,))
    old = time.time() - _KEPT_MAX_AGE_S - 10
    os.utime(stale / PYTEST_KEPT_MARKER, (old, old))
    assert sweep.tombstone_dead_runs(roots=[temp]) == 1
    assert fresh.exists()
    assert not stale.exists()
    assert (temp / "repo" / "pytest" / ".reap" / stale.name).is_dir()


def test_drain_stops_at_check_and_resumes(temp) -> None:
    reap = temp / "repo" / "pytest" / ".reap" / "9-abc123"
    reap.mkdir(parents=True)
    for name in ("a.txt", "b.txt", "c.txt"):
        (reap / name).write_text("x")
    names = ("a.txt", "b.txt", "c.txt")

    def check() -> bool:
        return sum((reap / name).exists() for name in names) < len(names)

    first = sweep.drain_tombstones(roots=[temp], check=check)
    assert first >= 1
    left = list(reap.rglob("*")) if reap.exists() else []
    assert left
    rest = sweep.drain_tombstones(roots=[temp], check=lambda: False)
    assert rest >= 1
    assert not reap.exists()
