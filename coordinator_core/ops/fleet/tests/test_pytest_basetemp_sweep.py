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

_OLD = time.time() - 10 * 3600.0


@pytest.fixture
def temp(tmp_path, monkeypatch):
    base = tmp_path / "Temp"
    base.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(base))
    return base / "coordinator"


@pytest.fixture
def registry(tmp_path):
    d = tmp_path / "registry"
    d.mkdir()
    return d


@pytest.fixture
def dead_pid():
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


def test_g5_deletes_dead_pid_dirs_across_repos(temp, registry, dead_pid):
    a = _mk(temp / "repo-a" / "pytest" / f"{dead_pid}-a1b2c3")
    b = _mk(temp / "repo-b" / "pytest" / f"{dead_pid}-d4e5f6")
    recs = sweep.sweep_dead_runs(registry_dir=registry)
    assert not a.exists() and not b.exists()
    assert sorted(r["action"] for r in recs) == ["deleted", "deleted"]


def test_g6_never_deletes_live_or_own_pid(temp, registry, dead_pid):
    own = _mk(temp / "repo" / "pytest" / f"{os.getpid()}-a1b2c3")
    parent = _mk(temp / "repo" / "pytest" / f"{os.getppid()}-a1b2c3")
    dead = _mk(temp / "repo" / "pytest" / f"{dead_pid}-a1b2c3")
    sweep.sweep_dead_runs(registry_dir=registry)
    assert not dead.exists()
    assert own.exists() and parent.exists()


def test_g7_leaves_marked_unparsable_and_fleet(temp, registry, dead_pid):
    marked = _mk(temp / "repo" / "pytest" / f"{dead_pid}-a1b2c3", files=(PYTEST_KEPT_MARKER,))
    odd = _mk(temp / "repo" / "pytest" / "abc-a1b2c3")
    fleet = _mk(temp / FLEET_DIRNAME / "pytest" / f"{dead_pid}-a1b2c3")
    dead = _mk(temp / "repo" / "pytest" / f"{dead_pid}-d4e5f6")
    sweep.sweep_dead_runs(registry_dir=registry)
    assert not dead.exists()
    assert marked.exists() and odd.exists() and fleet.exists()


def test_g8_dry_run_deletes_nothing_and_prints_would_delete(temp, dead_pid, capsys):
    run = _mk(temp / "repo" / "pytest" / f"{dead_pid}-a1b2c3")
    assert sweep.main(["--dry-run"]) == 0
    assert run.exists()
    recs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["action"] for r in recs] == ["would-delete"]
