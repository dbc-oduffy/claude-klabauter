"""`only_paths` / `removed_sink` on `sync_mirror` and `sync_flat_mirror`: a
sync visits exactly the named paths, and the deletion sink reports every
removal (or would-be removal under dry-run)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import publish_sync  # noqa: E402
from percolate.ignore import PercolateIgnoreMatcher  # noqa: E402

_NO_IGNORE = PercolateIgnoreMatcher([])


def _write(root: Path, rel: str, text: str = "x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_only_paths_copies_exactly_named_files_and_scans_nothing(tmp_path, monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "dst"
    dst.mkdir()
    for i in range(3000):
        _write(src, f"plug{i % 30}/f{i}.txt", str(i))
    scans = {"n": 0}
    real = os.scandir

    def counting(*a, **k):
        scans["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(os, "scandir", counting)
    changed: set[str] = set()
    synced, removed = publish_sync.sync_mirror(
        src, dst, _NO_IGNORE, False,
        only_paths=["plug0/f0.txt", "plug1/f1.txt", "plug2/f2.txt"],
        changed_paths=changed,
    )
    assert (synced, removed) == (3, 0)
    assert changed == {"plug0/f0.txt", "plug1/f1.txt", "plug2/f2.txt"}
    assert sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file()) == sorted(changed)
    assert scans["n"] < 30


def test_only_paths_deleted_at_source_lands_in_removed_sink(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    _write(dst, "plug/gone.txt")
    _write(dst, "plug/keep.txt")
    sink: set[str] = set()
    synced, removed = publish_sync.sync_mirror(
        src, dst, _NO_IGNORE, False, only_paths=["plug/gone.txt"], removed_sink=sink
    )
    assert (synced, removed) == (0, 1)
    assert sink == {"plug/gone.txt"}
    assert not (dst / "plug/gone.txt").exists()
    assert (dst / "plug/keep.txt").exists()


def test_only_paths_absent_everywhere_and_renamed_are_not_deleted(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    _write(dst, "renamed_dir/a.txt")
    _write(dst, "plug/renamed.txt")
    sink: set[str] = set()
    synced, removed = publish_sync.sync_mirror(
        src, dst, _NO_IGNORE, False,
        only_paths=["renamed_dir/a.txt", "plug/renamed.txt", "plug/never.txt"],
        removed_sink=sink,
        renamed_dir_names=frozenset({"renamed_dir"}),
        renamed_file_names=frozenset({"renamed.txt"}),
    )
    assert (synced, removed) == (0, 0)
    assert sink == set()


def test_dry_run_removed_sink_reports_orphan_sweep_without_touching_fs(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src, "plug/a.txt")
    _write(src, "top.txt")
    _write(dst, "plug/a.txt")
    _write(dst, "plug/stale.txt")
    _write(dst, "top.txt")
    _write(dst, "stale_top.txt")
    _write(dst, "olddir/x.txt")
    sink: set[str] = set()
    publish_sync.sync_mirror(
        src, dst, _NO_IGNORE, True,
        sweep_top_level_orphans=True, removed_sink=sink,
    )
    assert sink == {"plug/stale.txt", "stale_top.txt", "olddir/x.txt"}
    assert (dst / "plug/stale.txt").exists()
    assert (dst / "stale_top.txt").exists()
    assert (dst / "olddir/x.txt").exists()


def test_flat_only_paths_copies_and_deletes_named_only(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src, "a.txt", "new")
    _write(src, "b.txt")
    _write(dst, "gone.txt")
    _write(dst, "other.txt")
    sink: set[str] = set()
    synced, removed = publish_sync.sync_flat_mirror(
        src, dst, _NO_IGNORE, False, only_paths=["a.txt", "gone.txt"], removed_sink=sink
    )
    assert (synced, removed) == (1, 1)
    assert sink == {"gone.txt"}
    assert (dst / "a.txt").read_text() == "new"
    assert not (dst / "b.txt").exists()
    assert (dst / "other.txt").exists()


def test_flat_removed_sink_without_only_paths(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src, "a.txt")
    _write(dst, "a.txt")
    _write(dst, "stale.txt")
    sink: set[str] = set()
    publish_sync.sync_flat_mirror(src, dst, _NO_IGNORE, True, removed_sink=sink)
    assert sink == {"stale.txt"}
    assert (dst / "stale.txt").exists()


def test_none_defaults_keep_full_walk_behaviour(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src, "plug/a.txt")
    _write(src, "plug/b.txt")
    dst.mkdir()
    synced, removed = publish_sync.sync_mirror(src, dst, _NO_IGNORE, False)
    assert (synced, removed) == (2, 0)


def test_enforce_guards_makes_a_dry_run_abort_on_a_top_level_orphan(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(src, "kept/a.txt")
    _write(dst, "kept/a.txt")
    _write(dst, "stray/b.txt")
    sink: set[str] = set()
    publish_sync.sync_mirror(src, dst, _NO_IGNORE, True, removed_sink=sink)
    assert sink == {"stray/b.txt"}
    with pytest.raises(SystemExit) as exc:
        publish_sync.sync_mirror(src, dst, _NO_IGNORE, True, removed_sink=set(), enforce_guards=True)
    assert exc.value.code == 3
    assert (dst / "stray/b.txt").exists()
