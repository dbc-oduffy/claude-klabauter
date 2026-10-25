"""The run's one scoped worktree read: Snapshot answers, read_rel memoisation, declared-deletion filter, record-dir slice."""

from __future__ import annotations

import os

from coordinator_core.ops.dispatch_emit import worktree_snapshot as ws
from coordinator_core.ops.dispatch_emit.worktree_snapshot import (
    Snapshot,
    read_rel,
    record_dir_changes,
    tracked_at_head,
    worktree_snapshot,
)


def test_snapshot_covers_a_literal_and_anything_under_a_root():
    snap = Snapshot(set(), [], {"a.py"}, ["state/warp/r1"])
    assert snap.covers("a.py") and snap.covers("state/warp/r1") and snap.covers("state/warp/r1/x.json")
    assert not snap.covers("b.py") and not snap.covers("state/warp/r10/x.json")


def test_note_written_marks_the_path_changed_and_covered():
    snap = Snapshot(set(), [], set(), [])
    snap.note_written(["w.py"])
    assert snap.covers("w.py") and snap.changed == {"w.py"}


def test_changed_among_matches_files_and_directory_specs():
    snap = Snapshot({"d/a.py", "d2/b.py", "top.py"}, [], set(), [])
    assert snap.changed_among(["d", "top.py"]) == {"d/a.py", "top.py"}


def test_ignored_among_matches_files_and_ignored_directories():
    snap = Snapshot(set(), ["junk/", "one.log"], set(), [])
    assert snap.ignored_among(["junk/x.py", "one.log", "keep.py"]) == ["junk/x.py", "one.log"]


def test_nothing_asked_is_no_snapshot(tmp_path):
    assert worktree_snapshot(tmp_path, [], [], []) is None


def test_tracked_at_head_of_nothing_is_nothing(tmp_path):
    assert tracked_at_head(tmp_path, []) == []


def test_read_rel_serves_a_stale_free_read(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("one\n", encoding="utf-8", newline="")
    assert read_rel(tmp_path, "f.txt") == "one\n"
    target.write_text("two!\n", encoding="utf-8", newline="")
    st = os.stat(target)
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    assert read_rel(tmp_path, "f.txt") == "two!\n"


def test_read_rel_refuses_a_path_outside_the_root_and_a_missing_file(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    assert read_rel(root, "../outside.txt") is None
    assert read_rel(root, "absent.txt") is None


def test_read_rel_memo_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "_READ_REL_LIMIT", 2)
    monkeypatch.setattr(ws, "_READ_REL_MEMO", {})
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text(name, encoding="utf-8")
        assert read_rel(tmp_path, name) == name
    assert len(ws._READ_REL_MEMO) <= 2


def test_record_dir_changes_splits_present_from_deleted_and_skips_nested(tmp_path):
    rd = tmp_path / "state" / "warp" / "r1"
    (rd / "sub").mkdir(parents=True)
    (rd / "a.json").write_text("{}", encoding="utf-8")
    (rd / "sub" / "deep.json").write_text("{}", encoding="utf-8")
    changed = {
        "state/warp/r1/a.json", "state/warp/r1/gone.json", "state/warp/r1/sub/deep.json", "state/warp/r10/o.json",
    }
    present, deleted = record_dir_changes(tmp_path, "state/warp/r1/", changed)
    assert present == ["state/warp/r1/a.json"] and deleted == ["state/warp/r1/gone.json"]
