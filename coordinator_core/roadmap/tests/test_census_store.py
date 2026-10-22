"""Tests for coordinator_core.roadmap.census_store; every path is under tmp_path."""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.roadmap import census_store
from coordinator_core.roadmap.census_store import CensusStore, store_path


def _root(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _stat(tmp_path: Path, name: str = "f.txt"):
    p = tmp_path / name
    p.write_text("x", encoding="utf-8")
    return p, p.stat()


def test_round_trip_run_and_ident(tmp_path):
    root = _root(tmp_path)
    _, st = _stat(root)
    s = CensusStore.load(root)
    s.put_run("cmd", "k1", (0, "o", "e"), 12, 3)
    s.put_ident("f.txt", st, "abc", st.st_mtime_ns + 10)
    s.save()
    assert store_path(root).is_file()
    t = CensusStore.load(root)
    assert t.run("cmd") == ("k1", (0, "o", "e"), 12, 3)
    assert t.ident("f.txt", st) == "abc"


def test_newer_key_replaces_older(tmp_path):
    root = _root(tmp_path)
    s = CensusStore.load(root)
    s.put_run("cmd", "k1", (0, "", ""), 5, None)
    s.put_run("cmd", "k2", (1, "", "x"), 6, 2)
    s.save()
    assert CensusStore.load(root).run("cmd") == ("k2", (1, "", "x"), 6, 2)


def test_missing_corrupt_and_other_schema_load_empty(tmp_path):
    root = _root(tmp_path)
    assert CensusStore.load(root).run("cmd") is None
    p = store_path(root)
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert CensusStore.load(root).run("cmd") is None
    p.write_text('{"schema": 999, "runs": {"cmd": ["k", [0, "", ""], 1, 1]}, "idents": {}}',
                 encoding="utf-8")
    assert CensusStore.load(root).run("cmd") is None
    p.write_text('{"schema": 1, "runs": [], "idents": {}}', encoding="utf-8")
    assert CensusStore.load(root).run("cmd") is None


def test_load_never_raises_without_a_git_root(tmp_path):
    assert CensusStore.load(tmp_path / "nowhere").median_run_ms() is None


def test_ident_requires_same_stat_and_non_racy(tmp_path):
    root = _root(tmp_path)
    p, st = _stat(root)
    s = CensusStore()
    s.put_ident("f.txt", st, "abc", st.st_mtime_ns + 1)
    assert s.ident("f.txt", st) == "abc"

    class _St:
        def __init__(self, size, mtime_ns, ino):
            self.st_size, self.st_mtime_ns, self.st_ino = size, mtime_ns, ino

    assert s.ident("f.txt", _St(st.st_size + 1, st.st_mtime_ns, st.st_ino)) is None
    assert s.ident("f.txt", _St(st.st_size, st.st_mtime_ns + 1, st.st_ino)) is None
    assert s.ident("f.txt", _St(st.st_size, st.st_mtime_ns, st.st_ino + 1)) is None
    assert s.ident("other", st) is None
    s.put_ident("f.txt", st, "abc", st.st_mtime_ns)
    assert s.ident("f.txt", st) is None


def test_save_to_unwritable_directory_raises_nothing(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    s = CensusStore(blocker / "sub" / "census-store.json")
    s.put_run("cmd", "k", (0, "", ""), 1, 1)
    s.save()
    assert not (blocker / "sub").exists()


def test_save_without_change_writes_nothing(tmp_path):
    root = _root(tmp_path)
    s = CensusStore.load(root)
    s.put_run("cmd", "k", (0, "", ""), 1, 1)
    s.save()
    p = store_path(root)
    os.utime(p, ns=(1_000_000_000, 1_000_000_000))
    t = CensusStore.load(root)
    t.put_run("cmd", "k", (0, "", ""), 1, 1)
    t.save()
    assert p.stat().st_mtime_ns == 1_000_000_000
    CensusStore.load(root).save()
    assert p.stat().st_mtime_ns == 1_000_000_000


def test_median_run_ms(tmp_path):
    s = CensusStore()
    assert s.median_run_ms() is None
    for i, ms in enumerate((10, 30, 20)):
        s.put_run(f"c{i}", "k", (0, "", ""), ms, None)
    assert s.median_run_ms() == 20


def test_save_drops_identities_of_absent_paths(tmp_path):
    root = _root(tmp_path)
    p, st = _stat(root)
    s = CensusStore.load(root)
    s.put_ident("f.txt", st, "abc", st.st_mtime_ns + 5)
    s.put_ident("gone.txt", st, "def", st.st_mtime_ns + 5)
    s.save()
    t = CensusStore.load(root)
    assert t.ident("f.txt", st) == "abc"
    assert t.ident("gone.txt", st) is None


def test_generates_is_empty():
    assert census_store.GENERATES == []
