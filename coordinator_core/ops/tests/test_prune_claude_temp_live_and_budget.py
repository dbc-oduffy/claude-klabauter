"""prune_claude_temp: a live session's dir survives, and the scratch budget never takes in-flight work."""

from __future__ import annotations

import os
import time

from coordinator_core.ops import prune_claude_temp as pct

HOUR = 3600


def _write(path, size, age_s, now):
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


def test_live_session_dir_kept_past_retention_dead_one_removed(tmp_path, monkeypatch):
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


def test_unknown_liveness_keeps_every_session(tmp_path, monkeypatch):
    root = _claude_root(tmp_path, monkeypatch)
    now = time.time()
    _write(root / "proj" / "s1" / "scratchpad" / "work.txt", 1, 5 * 24 * HOUR, now)
    pct.prune(roots=[(root, 2, 2)], now=now, drain=True, live_session_ids=None)
    assert (root / "proj" / "s1" / "scratchpad" / "work.txt").exists()


def test_budget_deletes_oldest_idle_until_session_fits(tmp_path):
    root = tmp_path / "claude"
    now = time.time()
    pad = root / "proj" / "s1" / "scratchpad"
    _write(pad / "oldest.bin", 100, 10 * HOUR, now)
    _write(pad / "older.bin", 100, 5 * HOUR, now)
    _write(pad / "idle.bin", 100, 2 * HOUR, now)
    removed = pct._enforce_scratch_budget(root, 150, now, pct._Budget(None))
    assert removed == 2
    assert sorted(p.name for p in pad.iterdir()) == ["idle.bin"]


def test_tree_stats_of_a_loose_file_is_its_own_size_and_mtime(tmp_path):
    now = time.time()
    f = tmp_path / "loose.bin"
    _write(f, 123, 600, now)
    size, newest = pct._tree_stats(str(f), pct._Budget(None))
    assert size == 123
    assert abs(newest - (now - 600)) < 2


def test_loose_files_count_toward_the_budget(tmp_path):
    root = tmp_path / "claude"
    now = time.time()
    pad = root / "proj" / "s1" / "scratchpad"
    _write(pad / "a.bin", 100, 10 * HOUR, now)
    _write(pad / "b.bin", 100, 9 * HOUR, now)
    assert pct.over_budget_sessions(root, 150, pct._Budget(None)) == {str(root / "proj" / "s1"): 200}


def test_over_budget_session_keeps_fresh_loose_file_beside_old_large_dir(tmp_path):
    root = tmp_path / "claude"
    now = time.time()
    pad = root / "proj" / "s1" / "scratchpad"
    _write(pad / "bigdir" / "blob.bin", 1000, 10 * HOUR, now)
    _write(pad / "fresh.txt", 10, 5, now)
    removed = pct._enforce_scratch_budget(root, 100, now, pct._Budget(None))
    assert removed == 1
    assert sorted(p.name for p in pad.iterdir()) == ["fresh.txt"]


def test_over_budget_session_keeps_a_fresh_empty_dir(tmp_path):
    root = tmp_path / "claude"
    now = time.time()
    pad = root / "proj" / "s1" / "scratchpad"
    _write(pad / "old.bin", 1000, 10 * HOUR, now)
    (pad / "fresh_dir").mkdir()
    removed = pct._enforce_scratch_budget(root, 100, now, pct._Budget(None))
    assert removed == 1
    assert sorted(p.name for p in pad.iterdir()) == ["fresh_dir"]


def test_budget_never_takes_a_child_written_within_the_idle_window(tmp_path):
    root = tmp_path / "claude"
    now = time.time()
    pad = root / "proj" / "s1" / "scratchpad"
    _write(pad / "old.bin", 100, 10 * HOUR, now)
    _write(pad / "in_flight.bin", 500, 60, now)
    removed = pct._enforce_scratch_budget(root, 150, now, pct._Budget(None))
    assert removed == 1
    assert sorted(p.name for p in pad.iterdir()) == ["in_flight.bin"]
