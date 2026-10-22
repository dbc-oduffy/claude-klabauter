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


class _St:
    def __init__(self, size, mtime_ns, ino):
        self.st_size, self.st_mtime_ns, self.st_ino = size, mtime_ns, ino


def test_schema_one_file_loads_empty_runs_idents_and_matches(tmp_path):
    root = _root(tmp_path)
    p = store_path(root)
    p.parent.mkdir(parents=True)
    p.write_text(
        '{"schema": 1, "runs": {"cmd": ["k", [0, "o", ""], 1, 1]},'
        ' "idents": {"f.txt": [1, 2, 3, "abc", 9]}}',
        encoding="utf-8",
    )
    s = CensusStore.load(root)
    assert s.run("cmd") is None
    assert s.ident("f.txt", _St(1, 2, 3)) is None
    assert s.match("f.txt", _St(1, 2, 3), "pk") is None
    assert census_store.STORE_SCHEMA == 2


def test_schema_two_file_without_a_matches_map_loads_empty(tmp_path):
    root = _root(tmp_path)
    p = store_path(root)
    p.parent.mkdir(parents=True)
    p.write_text('{"schema": 2, "runs": {}, "idents": {}}', encoding="utf-8")
    assert CensusStore.load(root).match("f.txt", _St(1, 2, 3), "pk") is None


def test_match_round_trips_through_save(tmp_path):
    root = _root(tmp_path)
    _, st = _stat(root)
    s = CensusStore.load(root)
    s.put_match("f.txt", st, "pk1", (True, 3, 0), st.st_mtime_ns + 10)
    s.put_match("f.txt", st, "pk2", (False, 0, 0), st.st_mtime_ns + 11)
    s.save()
    t = CensusStore.load(root)
    assert t.match("f.txt", st, "pk1") == (True, 3, 0)
    assert t.match("f.txt", st, "pk2") == (False, 0, 0)
    assert t.match("f.txt", st, "other") is None
    assert t.match("absent.txt", st, "pk1") is None
    assert len(t._matches) == 1


def test_match_requires_same_stat_and_non_racy(tmp_path):
    root = _root(tmp_path)
    _, st = _stat(root)
    s = CensusStore()
    s.put_match("f.txt", st, "pk", (True, 1, 0), st.st_mtime_ns + 1)
    assert s.match("f.txt", st, "pk") == (True, 1, 0)
    assert s.match("f.txt", _St(st.st_size + 1, st.st_mtime_ns, st.st_ino), "pk") is None
    assert s.match("f.txt", _St(st.st_size, st.st_mtime_ns + 1, st.st_ino), "pk") is None
    assert s.match("f.txt", _St(st.st_size, st.st_mtime_ns, st.st_ino + 1), "pk") is None
    racy = CensusStore()
    racy.put_match("f.txt", st, "pk", (True, 1, 0), st.st_mtime_ns)
    assert racy.match("f.txt", st, "pk") is None


def test_changed_stat_replaces_the_files_whole_record(tmp_path):
    s = CensusStore()
    old = _St(1, 10, 5)
    new = _St(2, 20, 5)
    s.put_match("f", old, "a", (True, 1, 0), 100)
    s.put_match("f", old, "b", (True, 2, 0), 100)
    s.put_match("f", new, "a", (False, 0, 0), 200)
    assert s.match("f", old, "a") is None
    assert s.match("f", new, "a") == (False, 0, 0)
    assert s.match("f", new, "b") is None
    assert s._match_entries == 1


def test_racy_record_is_not_extended_by_a_later_clean_put(tmp_path):
    s = CensusStore()
    st = _St(1, 100, 5)
    s.put_match("f", st, "a", (True, 1, 0), 100)
    s.put_match("f", st, "b", (True, 1, 0), 500)
    assert s.match("f", st, "a") is None
    assert s.match("f", st, "b") == (True, 1, 0)


def test_entries_past_the_limit_are_refused_and_existing_ones_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(census_store, "MATCH_ENTRY_LIMIT", 3)
    s = CensusStore()
    st = _St(1, 10, 5)
    for i in range(5):
        s.put_match(f"f{i}", st, "a", (True, 1, 0), 100)
    assert s._match_entries == 3
    assert s.match("f0", st, "a") == (True, 1, 0)
    assert s.match("f4", st, "a") is None
    s.put_match("f0", st, "a", (False, 0, 0), 100)
    assert s.match("f0", st, "a") == (False, 0, 0)
    s.put_match("f0", st, "b", (True, 1, 0), 100)
    assert s.match("f0", st, "b") is None


def test_save_drops_match_records_of_absent_paths(tmp_path):
    root = _root(tmp_path)
    p, st = _stat(root)
    s = CensusStore.load(root)
    s.put_match("f.txt", st, "pk", (True, 1, 0), st.st_mtime_ns + 5)
    s.put_match("gone.txt", st, "pk", (True, 1, 0), st.st_mtime_ns + 5)
    s.save()
    t = CensusStore.load(root)
    assert t.match("f.txt", st, "pk") == (True, 1, 0)
    assert t.match("gone.txt", st, "pk") is None
    assert t._match_entries == 1


def test_a_match_alone_marks_the_store_dirty(tmp_path):
    root = _root(tmp_path)
    _, st = _stat(root)
    s = CensusStore.load(root)
    s.put_match("f.txt", st, "pk", (True, 1, 0), st.st_mtime_ns + 5)
    s.save()
    assert store_path(root).is_file()
    os.utime(store_path(root), ns=(1_000_000_000, 1_000_000_000))
    t = CensusStore.load(root)
    t.put_match("f.txt", st, "pk", (True, 1, 0), st.st_mtime_ns + 5)
    t.save()
    assert store_path(root).stat().st_mtime_ns == 1_000_000_000


def test_store_at_the_entry_limit_loads_and_saves_inside_its_share_of_the_bar(tmp_path):
    import time

    root = _root(tmp_path)
    patterns = [f"{i:016x}" for i in range(12)]
    files = census_store.MATCH_ENTRY_LIMIT // len(patterns)
    data_dir = root / "d"
    data_dir.mkdir()
    s = CensusStore.load(root)
    for i in range(files):
        p = data_dir / f"f{i}.py"
        p.write_bytes(b"x")
        os.utime(p, ns=(1_000_000_000, 1_000_000_000))
        st = p.stat()
        for pk in patterns:
            s.put_match(f"d/f{i}.py", st, pk, (True, 3, 0), 2_000_000_000)
    assert s._match_entries == files * len(patterns)
    started = time.process_time()
    s.save()
    loaded = CensusStore.load(root)
    spent = time.process_time() - started
    assert len(loaded._matches) == files
    assert spent < 0.25, spent
    size = store_path(root).stat().st_size
    assert size < 8_000_000, size


def test_generates_is_empty():
    assert census_store.GENERATES == []
