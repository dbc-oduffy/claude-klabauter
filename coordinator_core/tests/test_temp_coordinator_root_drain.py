"""Falsifier: default-argument ``prune()`` drains dead-pid pytest run dirs under ``<temp>/coordinator``."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import time
from pathlib import Path

import pytest

from coordinator_core.group_em import session_registry
from coordinator_core.ops import prune_claude_temp as pct

DEAD_RUNS = 50
DEFAULT_PASSES = 10
LIVE_PID = 91000001
_CENSUS = Path(__file__).resolve().parents[1] / "benchmarks" / "temp_coordinator_root_census.py"


def _load_census():
    spec = importlib.util.spec_from_file_location("temp_coordinator_root_census", _CENSUS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_dir(pdir: Path, pid: int, files: int = 4) -> Path:
    run = pdir / f"{pid}-abc123"
    for i in range(files):
        leaf = run / "sub" / "deeper" / f"f{i}.txt"
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_text("x")
    return run


@pytest.fixture
def root_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    temp = tmp_path / "systemp"
    temp.mkdir()
    from coordinator_core.warm import breadcrumb

    monkeypatch.setattr(breadcrumb, "svc_dir", lambda engine_root=None: tmp_path / "svc")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(session_registry, "pid_alive", lambda pid: pid == LIVE_PID)
    pdir = temp / "coordinator" / "repoA" / "pytest"
    old = time.time() - 7200
    dead = [_run_dir(pdir, 90000000 + i) for i in range(DEAD_RUNS)]
    for run in dead:
        os.utime(run, (old, old))
    survivors = [_run_dir(pdir, LIVE_PID), _run_dir(pdir, os.getpid())]
    return temp / "coordinator", pdir, dead, survivors


def test_default_prune_drains_dead_pid_run_dirs_and_spares_live_owners(root_fixture) -> None:
    root, pdir, dead, survivors = root_fixture
    assert all(d.exists() for d in dead)

    for _ in range(DEFAULT_PASSES):
        pct.prune()

    left = [d.name for d in dead if d.exists()]
    assert not left, f"{len(left)} of {DEAD_RUNS} dead-pid run dirs remain after {DEFAULT_PASSES} default prune runs"
    reap = pdir / ".reap"
    assert not reap.exists() or not any(reap.iterdir())
    assert all(s.exists() for s in survivors)
    totals = _load_census().census([root])["totals"]
    assert totals["pytest"]["dead_owner"] == 0
    assert totals["pytest"]["entries"] >= len(survivors)
