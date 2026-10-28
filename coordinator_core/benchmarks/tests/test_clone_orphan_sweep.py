"""clone_orphan_sweep signals only orphaned processes rooted under scratch/benchmark-clones."""

from __future__ import annotations

import subprocess
import sys

import psutil
import pytest

from coordinator_core.benchmarks import clone_orphan_sweep as cos

_SLEEP = "import time; time.sleep(120)"
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _spawn(cwd):
    return subprocess.Popen([sys.executable, "-c", _SLEEP], cwd=str(cwd), creationflags=_NOWIN)


@pytest.fixture
def procs():
    spawned = []
    yield spawned
    for p in spawned:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=10)


def _clone(tmp_path, name="c1"):
    d = tmp_path / "scratch" / "benchmark-clones" / name / "clone"
    d.mkdir(parents=True)
    return d


def _alive(p):
    return p.poll() is None


def test_dead_parent_orphan_is_terminated(tmp_path, procs):
    child = _spawn(_clone(tmp_path))
    procs.append(child)
    reaped = cos.sweep(str(tmp_path), parent_dead=lambda proc: proc.pid == child.pid)
    child.wait(timeout=10)
    assert child.pid in reaped
    assert not _alive(child)


def test_clone_dir_removed_is_orphan(tmp_path, procs):
    clone = _clone(tmp_path)
    child = _spawn(clone)
    procs.append(child)
    # A cwd cannot be removed on Windows; point the cmdline at a vanished clone instead.
    gone = tmp_path / "scratch" / "benchmark-clones" / "gone" / "x.py"
    child2 = subprocess.Popen(
        [sys.executable, "-c", _SLEEP, str(gone)], cwd=str(tmp_path), creationflags=_NOWIN
    )
    procs.append(child2)
    reaped = cos.sweep(str(tmp_path), parent_dead=lambda proc: False)
    child2.wait(timeout=10)
    assert child2.pid in reaped
    assert not _alive(child2)
    assert _alive(child)


def test_live_parent_live_tree_left_alone(tmp_path, procs):
    child = _spawn(_clone(tmp_path))
    procs.append(child)
    assert cos.sweep(str(tmp_path), parent_dead=lambda proc: False) == []
    assert _alive(child)


def test_process_outside_clones_never_considered(tmp_path, procs):
    outside = tmp_path / "scratch" / "other"
    outside.mkdir(parents=True)
    child = _spawn(outside)
    procs.append(child)
    assert cos.sweep(str(tmp_path), parent_dead=lambda proc: True) == []
    assert _alive(child)


def test_live_parent_in_clone_with_orphan_left_alone(tmp_path, procs):
    clone = _clone(tmp_path)
    live = _spawn(clone)
    orphan = _spawn(clone)
    procs.extend([live, orphan])
    called = []
    reaped = cos.sweep(
        str(tmp_path),
        parent_dead=lambda proc: proc.pid == orphan.pid,
        stop_pair=lambda root: called.append(root),
    )
    orphan.wait(timeout=10)
    assert reaped == [orphan.pid]
    assert not _alive(orphan)
    assert _alive(live)
    assert called == []


def test_real_parent_predicate_keeps_live_parent(tmp_path, procs):
    child = _spawn(_clone(tmp_path))
    procs.append(child)
    assert psutil.pid_exists(child.pid)
    assert cos.sweep(str(tmp_path)) == []
    assert _alive(child)
