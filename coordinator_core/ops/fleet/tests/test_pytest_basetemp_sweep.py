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
