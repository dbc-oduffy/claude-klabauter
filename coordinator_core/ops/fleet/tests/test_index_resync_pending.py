"""Tests for the pending-resync record store."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.fleet import _index_resync_pending as mod
from coordinator_core.ops.fleet._index_resync_pending import (
    PendingResync,
    discharge,
    list_pending,
    pending_dir,
    record_pending,
)


def _rec(src="state/a.md", dst="archive/a.md", blob="abc", cls=""):
    return PendingResync(src, dst, "cid-" + src, blob, "archive_and_commit",
                         "2026-09-30T00:00:00Z", cls)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_round_trip_and_discharge(tmp_path):
    root = _repo(tmp_path)
    rec = _rec()
    record_pending(root, [rec])
    assert list_pending(root) == [rec]
    discharge(root, rec)
    discharge(root, rec)
    assert list_pending(root) == []


def test_idempotent_rerecord_overwrites(tmp_path):
    root = _repo(tmp_path)
    record_pending(root, [_rec()])
    updated = _rec(cls="contested")
    record_pending(root, [updated])
    assert list_pending(root) == [updated]
    assert len(list(pending_dir(root).glob("*.json"))) == 1


def test_absent_dir(tmp_path):
    assert list_pending(_repo(tmp_path)) == []


def test_corrupt_file_skipped(tmp_path):
    root = _repo(tmp_path)
    good = _rec()
    record_pending(root, [good])
    (pending_dir(root) / "bad.json").write_text("{not json", encoding="utf-8")
    (pending_dir(root) / "wrong.json").write_text('{"x": 1}', encoding="utf-8")
    assert list_pending(root) == [good]


def test_dot_git_as_file(tmp_path):
    real = tmp_path / "gitstore" / "wt"
    real.mkdir(parents=True)
    root = tmp_path / "work"
    root.mkdir()
    (root / ".git").write_text(f"gitdir: {real}\n", encoding="utf-8")
    assert pending_dir(root) == real / mod.PENDING_DIRNAME
    rec = _rec()
    record_pending(root, [rec])
    assert list_pending(root) == [rec]
    assert not (root / "state").exists()


def test_dot_git_file_relative_gitdir(tmp_path):
    (tmp_path / "rel").mkdir()
    (tmp_path / ".git").write_text("gitdir: rel\n", encoding="utf-8")
    assert pending_dir(tmp_path) == tmp_path / "rel" / mod.PENDING_DIRNAME


def test_record_failure_never_raises(tmp_path):
    root = _repo(tmp_path)
    (root / ".git" / mod.PENDING_DIRNAME).write_text("file blocks dir", encoding="utf-8")
    record_pending(root, [_rec()])


def test_module_source_has_no_subprocess():
    assert "subprocess" not in Path(mod.__file__).read_text(encoding="utf-8")
