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
