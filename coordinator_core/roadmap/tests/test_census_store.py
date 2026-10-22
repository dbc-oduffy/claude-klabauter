"""Tests for coordinator_core.roadmap.census_store (schema 3); every path is under tmp_path."""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.roadmap import census_store
from coordinator_core.roadmap.census_store import CensusStore, store_path


def _root(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _shard_dir(root: Path) -> Path:
    return store_path(root).with_suffix("")


def test_schema_is_three():
    assert census_store.STORE_SCHEMA == 3


def test_round_trip_every_pinned_api(tmp_path):
    root = _root(tmp_path)
    s = CensusStore.load(root)
    s.put_run("cmd", "k1", (0, "o", "e"), 12, 3)
    s.put_index_fps("c" * 40, {"a.py": "oid1:100644"})
    s.put_matched("pk1", {"o1": 3, "o2": 0})
    s.put_screened("grep x", None)
    s.put_screened("rm y", "refused")
    s.put_cursor("cmd")
    s.put_over_bar("slow", 900)
    s.save()
    census_store._SEEN.clear()
    t = CensusStore.load(root)
    assert t.run("cmd") == ("k1", (0, "o", "e"), 12, 3)
    assert t.index_fps("c" * 40) == {"a.py": "oid1:100644"}
    assert t.index_fps("d" * 40) is None
    assert t.matched("pk1", "o1") == 3
    assert t.matched("pk1", "o2") == 0
    assert t.matched("pk1", "o3") is None
    assert t.matched("other", "o1") is None
    assert t.screened("grep x") == (None,)
    assert t.screened("rm y") == ("refused",)
    assert t.screened("unseen") is None
    assert t.cursor() == "cmd"
    assert t.over_bar("slow") == 900
    assert t.over_bar("fast") is None
    assert t.load_cost_ms() >= 0
    t.put_cursor(None)
    t.save()
    census_store._SEEN.clear()
    assert CensusStore.load(root).cursor() is None


def test_one_checksum_at_a_time(tmp_path):
    s = CensusStore()
    s.put_index_fps("a", {"x": "1"})
    s.put_index_fps("b", {"y": "2"})
    assert s.index_fps("a") is None
    assert s.index_fps("b") == {"y": "2"}


def test_newer_key_replaces_older(tmp_path):
    root = _root(tmp_path)
    s = CensusStore.load(root)
    s.put_run("cmd", "k1", (0, "", ""), 5, None)
    s.put_run("cmd", "k2", (1, "", "x"), 6, 2)
    s.save()
    census_store._SEEN.clear()
    assert CensusStore.load(root).run("cmd") == ("k2", (1, "", "x"), 6, 2)


def test_a_later_put_run_clears_the_over_bar_marker(tmp_path):
    s = CensusStore()
    s.put_over_bar("cmd", 700)
    s.put_run("cmd", "k", (0, "", ""), 5, None)
    assert s.over_bar("cmd") is None


def test_schema_one_and_two_json_and_garbage_load_empty(tmp_path):
    root = _root(tmp_path)
    p = store_path(root)
    p.parent.mkdir(parents=True)
    for body in ('{not json', '{"schema": 1, "runs": {"cmd": ["k", [0, "", ""], 1, 1]}}',
                 '{"schema": 2, "runs": {"cmd": ["k", [0, "", ""], 1, 1]}, "idents": {},'
                 ' "matches": {}}'):
        p.write_text(body, encoding="utf-8")
        s = CensusStore.load(root)
        assert s.run("cmd") is None
        assert s.median_run_ms() is None


def test_first_save_removes_the_legacy_json(tmp_path):
    root = _root(tmp_path)
    p = store_path(root)
    p.parent.mkdir(parents=True)
    p.write_text('{"schema": 2}', encoding="utf-8")
    s = CensusStore.load(root)
    s.put_run("cmd", "k", (0, "", ""), 1, 1)
    s.save()
    assert not p.exists()
    assert (_shard_dir(root) / "root.marshal").is_file()


def test_cache_tag_mismatch_and_corrupt_shard_load_empty(tmp_path, monkeypatch):
    root = _root(tmp_path)
    s = CensusStore.load(root)
    s.put_run("cmd", "k", (0, "", ""), 1, 1)
    s.put_matched("pk", {"o": 1})
    s.save()
    census_store._SEEN.clear()
    monkeypatch.setattr(census_store, "_header", lambda: (3, "other-vm"))
    t = CensusStore.load(root)
    assert t.run("cmd") is None
    assert t.matched("pk", "o") is None
    monkeypatch.undo()
    census_store._SEEN.clear()
    (_shard_dir(root) / "p-pk.marshal").write_bytes(b"garbage")
    u = CensusStore.load(root)
    assert u.run("cmd") is not None
    assert u.matched("pk", "o") is None


def test_load_never_raises_without_a_git_root(tmp_path):
    assert CensusStore.load(tmp_path / "nowhere").median_run_ms() is None


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
    p = _shard_dir(root) / "root.marshal"
    os.utime(p, ns=(1_000_000_000, 1_000_000_000))
    t = CensusStore.load(root)
    t.put_run("cmd", "k", (0, "", ""), 1, 1)
    t.save()
    assert p.stat().st_mtime_ns == 1_000_000_000


def test_median_run_ms():
    s = CensusStore()
    assert s.median_run_ms() is None
    for i, ms in enumerate((10, 30, 20)):
        s.put_run(f"c{i}", "k", (0, "", ""), ms, None)
    assert s.median_run_ms() == 20


def test_one_put_matched_rewrites_exactly_one_pattern_shard(tmp_path):
    root = _root(tmp_path)
    s = CensusStore.load(root)
    s.put_matched("pa", {f"o{i}": 1 for i in range(2000)})
    s.put_matched("pb", {"x": 1})
    s.save()
    d = _shard_dir(root)
    files = sorted(p.name for p in d.iterdir())
    assert files == ["p-pa.marshal", "p-pb.marshal", "root.marshal"]
    for p in d.iterdir():
        os.utime(p, ns=(1_000_000_000, 1_000_000_000))
    census_store._SEEN.clear()
    t = CensusStore.load(root)
    t.put_matched("pb", {"y": 2})
    t.save()
    changed = sorted(p.name for p in d.iterdir() if p.stat().st_mtime_ns != 1_000_000_000)
    assert changed == ["p-pb.marshal"]
    assert sorted(p.name for p in d.iterdir()) == files
    census_store._SEEN.clear()
    u = CensusStore.load(root)
    assert u.matched("pb", "x") == 1 and u.matched("pb", "y") == 2
    assert u.matched("pa", "o1999") == 1


def test_in_process_reuse_skips_the_reread_and_rereads_after_an_external_write(
        tmp_path, monkeypatch):
    root = _root(tmp_path)
    s = CensusStore.load(root)
    s.put_matched("pk", {"o": 1})
    s.save()
    reads = []
    real = census_store._read_shard
    monkeypatch.setattr(census_store, "_read_shard", lambda p: reads.append(p) or real(p))
    t = CensusStore.load(root)
    assert t.matched("pk", "o") == 1
    assert reads == []
    census_store._SEEN.clear()
    u = CensusStore.load(root)
    assert u.matched("pk", "o") == 1
    assert len(reads) == 2
    import marshal

    (_shard_dir(root) / "p-pk.marshal").write_bytes(
        marshal.dumps((census_store._header(), {"o": 7, "o2": 2})))
    reads.clear()
    v = CensusStore.load(root)
    assert v.matched("pk", "o") == 7
    assert len(reads) == 1


def test_an_unsaved_mutation_never_leaks_through_the_shared_copy(tmp_path):
    root = _root(tmp_path)
    s = CensusStore.load(root)
    s.put_matched("pk", {"o": 1})
    s.save()
    t = CensusStore.load(root)
    t.put_matched("pk", {"o": 9})
    assert CensusStore.load(root).matched("pk", "o") == 1


def test_gc_oids_drops_dead_oids(tmp_path):
    root = _root(tmp_path)
    s = CensusStore.load(root)
    s.put_matched("pk", {"live": 1, "dead": 2})
    s.gc_oids({"live"})
    s.save()
    census_store._SEEN.clear()
    t = CensusStore.load(root)
    assert t.matched("pk", "live") == 1
    assert t.matched("pk", "dead") is None


def test_the_stat_keyed_shims_are_gone():
    for name in ("ident", "put_ident", "match", "put_match"):
        assert not hasattr(CensusStore, name), name


def _plan_file(root: Path, mtime_ns: int) -> os.stat_result:
    plan = root / "p.md"
    plan.write_text("---\ncensus: []\n---\n", encoding="utf-8")
    os.utime(plan, ns=(mtime_ns, mtime_ns))
    return plan.stat()


def test_a_parsed_census_round_trips_under_its_stat_and_a_changed_stat_misses(tmp_path):
    root = _root(tmp_path)
    st = _plan_file(root, 1_700_000_000_123_456_789)
    census = [{"question": "q", "command": "ls", "result": "1"}]
    s = CensusStore.load(root)
    assert s.parsed("p.md", st) is None
    s.put_parsed("p.md", st, census, st.st_mtime_ns + 5)
    assert s.parsed("p.md", st) == census
    s.save()
    census_store._SEEN.clear()
    t = CensusStore.load(root)
    assert t.parsed("p.md", st) == census
    st2 = _plan_file(root, 1_700_000_001_000_000_001)
    assert t.parsed("p.md", st2) is None
    assert (_shard_dir(root) / "plans.marshal").exists()


def test_a_whole_second_mtime_read_within_two_seconds_is_not_kept(tmp_path):
    root = _root(tmp_path)
    st = _plan_file(root, 1_700_000_000 * 10**9)
    s = CensusStore.load(root)
    s.put_parsed("p.md", st, [{"command": "ls"}], st.st_mtime_ns + 10**9)
    assert s.parsed("p.md", st) is None
    s.put_parsed("p.md", st, [{"command": "ls"}], st.st_mtime_ns + 3 * 10**9)
    assert s.parsed("p.md", st) == [{"command": "ls"}]


def test_a_census_marshal_cannot_hold_is_not_kept_and_save_survives(tmp_path):
    import datetime

    root = _root(tmp_path)
    st = _plan_file(root, 1_700_000_000_123_456_789)
    s = CensusStore.load(root)
    s.put_parsed("p.md", st, [{"result": datetime.date(2026, 1, 1)}], st.st_mtime_ns + 5)
    assert s.parsed("p.md", st) is None
    s.put_cursor("x")
    s.save()
    census_store._SEEN.clear()
    assert CensusStore.load(root).cursor() == "x"


def test_an_unsaved_parsed_census_never_leaks_through_the_shared_copy(tmp_path):
    root = _root(tmp_path)
    st = _plan_file(root, 1_700_000_000_123_456_789)
    s = CensusStore.load(root)
    s.put_parsed("p.md", st, [{"command": "a"}], st.st_mtime_ns + 5)
    s.save()
    t = CensusStore.load(root)
    t.put_parsed("p.md", st, [{"command": "b"}], st.st_mtime_ns + 5)
    assert CensusStore.load(root).parsed("p.md", st) == [{"command": "a"}]


def test_declined_commands_are_kept_for_one_checksum_at_a_time(tmp_path):
    root = _root(tmp_path)
    s = CensusStore.load(root)
    assert s.declined("a") == frozenset()
    s.put_declined("a", {"x", "y"})
    assert s.declined("a") == {"x", "y"}
    assert s.declined("b") == frozenset()
    s.save()
    census_store._SEEN.clear()
    assert CensusStore.load(root).declined("a") == {"x", "y"}
    s.put_declined("b", {"z"})
    assert s.declined("a") == frozenset() and s.declined("b") == {"z"}


def test_an_in_process_run_marker_stays_out_of_the_median():
    s = CensusStore()
    s.put_run("a", "k", (0, "1\n", ""), census_store.INPROC_RUN_MS, 2)
    assert s.median_run_ms() is None
    s.put_run("b", "k", (0, "1\n", ""), 40, 2)
    assert s.median_run_ms() == 40


def test_matched_writes_moves_only_when_a_shard_changes():
    s = CensusStore()
    assert s.matched_writes == 0
    s.put_matched("pk", {"o": 1})
    assert s.matched_writes == 1
    s.put_matched("pk", {"o": 1})
    assert s.matched_writes == 1
    s.put_matched("pk", {"o": 2})
    assert s.matched_writes == 2


def test_generates_is_empty():
    assert census_store.GENERATES == []
