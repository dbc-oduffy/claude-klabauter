"""halted_withheld_index.entries: whole-index snapshot keyed on the index dir's mtime."""

import os
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx


def _age(root: Path) -> None:
    """Move the index dir mtime out of the settle window so the listing is kept."""
    d = idx._dir(root)
    old = os.stat(d).st_mtime_ns - 10_000_000_000
    os.utime(d, ns=(old, old))


def _seed(root: Path) -> None:
    idx.rebuild(root)
    idx.add_run(root, "run-a", ["a.py"])
    idx.add_run(root, "run-b", ["b.py"])
    _age(root)


def _count_parses(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []
    real = idx._parse_file
    monkeypatch.setattr(idx, "_parse_file", lambda f: calls.append(str(f)) or real(f))
    return calls


def test_second_entries_call_stats_only_the_index_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path)
    first = idx.entries(tmp_path)
    assert sorted(first) == ["run-a", "run-b"]
    stats: list = []
    real_stat = os.stat
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: stats.append(os.fspath(p)) or real_stat(p, *a, **k))
    scans: list = []
    real_scandir = os.scandir
    monkeypatch.setattr(os, "scandir", lambda p: scans.append(p) or real_scandir(p))
    assert idx.entries(tmp_path) == first
    assert stats == [os.fspath(idx._dir(tmp_path))]
    assert scans == []


def test_peer_atomic_write_is_seen(tmp_path: Path) -> None:
    from coordinator_core.atomic_replace import atomic_write_bytes

    _seed(tmp_path)
    assert "run-c" not in idx.entries(tmp_path)
    atomic_write_bytes(idx._dir(tmp_path) / "run-c.json", b'{"paths": ["c.py"]}')  # not via _store
    assert idx.entries(tmp_path)["run-c"].paths == ("c.py",)
    _age(tmp_path)
    idx.entries(tmp_path)
    atomic_write_bytes(idx._dir(tmp_path) / "run-c.json", b'{"paths": ["c2.py"]}')
    assert idx.entries(tmp_path)["run-c"].paths == ("c2.py",)


def test_peer_delete_is_seen(tmp_path: Path) -> None:
    _seed(tmp_path)
    idx.entries(tmp_path)
    (idx._dir(tmp_path) / "run-b.json").unlink()
    assert sorted(idx.entries(tmp_path)) == ["run-a"]


def test_store_and_remove_invalidate_the_snapshot(tmp_path: Path) -> None:
    _seed(tmp_path)
    idx.entries(tmp_path)
    idx.add_run(tmp_path, "run-a", ["a2.py"])
    assert idx.entries(tmp_path)["run-a"].paths == ("a2.py",)
    _age(tmp_path)
    idx.entries(tmp_path)
    idx.remove_run(tmp_path, "run-b")
    assert sorted(idx.entries(tmp_path)) == ["run-a"]


def test_listing_inside_the_settle_window_is_not_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    idx.rebuild(tmp_path)
    idx.add_run(tmp_path, "run-a", ["a.py"])  # dir mtime is now: inside the window
    idx.entries(tmp_path)
    scans: list = []
    real_scandir = os.scandir
    monkeypatch.setattr(os, "scandir", lambda p: scans.append(p) or real_scandir(p))
    idx.entries(tmp_path)
    assert len(scans) == 1


def test_a_deadline_truncated_listing_is_not_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path)
    ticks = iter(range(0, 10**9, 10**6))
    monkeypatch.setattr(idx, "_clock", lambda: next(ticks))  # each read jumps past the deadline
    assert idx.entries(tmp_path) == {}
    monkeypatch.undo()
    assert sorted(idx.entries(tmp_path)) == ["run-a", "run-b"]
