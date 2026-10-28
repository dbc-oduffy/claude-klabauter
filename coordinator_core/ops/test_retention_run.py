"""Tests for coordinator_core.ops.retention_run."""

from __future__ import annotations

import argparse
import importlib.util
import os
import time
import uuid
from pathlib import Path

from coordinator_core.ops import retention_run

MB = 1024 * 1024
HOUR = 3600


def _file(path: Path, size: int, age_h: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)
    t = time.time() - age_h * HOUR
    os.utime(path, (t, t))
    return path


def _fixture(home: Path):
    repo = home / "projects" / "r"
    ended, live = str(uuid.uuid4()), str(uuid.uuid4())
    old = _file(repo / f"{ended}.jsonl", 10, 49)
    big = _file(repo / f"{str(uuid.uuid4())}.jsonl", 251 * MB, 1)
    keep_live = _file(repo / f"{live}.jsonl", 300 * MB, 100)
    return old, big, keep_live, live


def test_run_deletes_ended_keeps_live_and_stamps(tmp_path):
    home = tmp_path / "home"
    old, big, keep_live, live = _fixture(home)
    out = retention_run.run_retention(
        claude_home=home, temp_root=tmp_path / "t", live_session_ids=frozenset({live}),
    )
    assert out["ran"] is True
    assert not old.exists() and not big.exists()
    assert keep_live.exists()
    assert (home / retention_run.STAMP_NAME).exists()
    assert not (home / retention_run.LOCK_NAME).exists()


def test_fresh_stamp_debounces_and_force_overrides(tmp_path):
    home = tmp_path / "home"
    old, _big, _kl, live = _fixture(home)
    (home / retention_run.STAMP_NAME).touch()
    kw = dict(claude_home=home, temp_root=tmp_path / "t", live_session_ids=frozenset({live}))
    assert retention_run.run_retention(**kw)["ran"] is False
    assert old.exists()
    assert retention_run.run_retention(force=True, **kw)["ran"] is True
    assert not old.exists()


def test_held_lock_skips_and_stale_lock_is_taken(tmp_path):
    home = tmp_path / "home"
    old, _big, _kl, live = _fixture(home)
    lock = home / retention_run.LOCK_NAME
    lock.touch()
    kw = dict(claude_home=home, temp_root=tmp_path / "t", live_session_ids=frozenset({live}))
    assert retention_run.run_retention(**kw)["ran"] is False
    assert old.exists()
    t = time.time() - 2 * HOUR
    os.utime(lock, (t, t))
    assert retention_run.run_retention(**kw)["ran"] is True
    assert not old.exists()


def test_unresolved_liveness_keeps_every_transcript(tmp_path, monkeypatch):
    from coordinator_core.ops import cruft_sweep

    home = tmp_path / "home"
    old, big, keep_live, _live = _fixture(home)
    monkeypatch.setattr(cruft_sweep, "box_live_session_ids", lambda: None)
    out = retention_run.run_retention(claude_home=home, temp_root=tmp_path / "t")
    assert out["ran"] is True
    assert old.exists() and big.exists() and keep_live.exists()


def test_apply_false_deletes_nothing_and_does_not_stamp(tmp_path):
    home = tmp_path / "home"
    old, _big, _kl, live = _fixture(home)
    retention_run.run_retention(
        claude_home=home, temp_root=tmp_path / "t", live_session_ids=frozenset({live}), apply=False,
    )
    assert old.exists()
    assert not (home / retention_run.STAMP_NAME).exists()


def test_spawn_if_due_only_spawns_when_due(tmp_path):
    calls = []

    def spawn(root, script, args):
        calls.append((root, script))
        return True

    assert retention_run.spawn_if_due("R", claude_home=tmp_path, spawn=spawn) is True
    (tmp_path / retention_run.STAMP_NAME).touch()
    assert retention_run.spawn_if_due("R", claude_home=tmp_path, spawn=spawn) is False
    assert calls == [("R", "coordinator_core/ops/retention_run.py")]


def test_spawn_if_due_never_raises(tmp_path):
    def boom(*a):
        raise RuntimeError("x")

    assert retention_run.spawn_if_due("R", claude_home=tmp_path, spawn=boom) is False


def test_governor_loop_fires_retention_on_first_tick(monkeypatch):
    src = Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "group-em-memory-governor.py"
    spec = importlib.util.spec_from_file_location("gov_for_retention", src)
    gov = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(__import__("sys").modules, "gov_for_retention", gov)
    spec.loader.exec_module(gov)
    fired = []
    args = argparse.Namespace(interval=0, max_pauses=3, resume_after=6, psi_full=10.0,
                              avail_pct=5.0, runaway_mb=0, runaway_every=6)
    gov.run(args, emit=lambda _l: None, sample=lambda: False, procs_fn=lambda: [],
            sleep=lambda _s: None, max_ticks=3, retention_fn=lambda: fired.append(1))
    assert fired == [1]
