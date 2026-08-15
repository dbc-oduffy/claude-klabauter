"""
Tests for coordinator_core.ops.session.reap._prune_stale_agent_archive (sub-reap
(iv)): the retention prune for .archive/_agents-* entries left behind by
sub-reap (ii), which moves stale agent dirs into .archive/ but never deletes
them (the unbounded-growth leak this sub-reap closes).

Spec backlink: dispatch brief "Close an unbounded-growth leak that is
unambiguously this repo's own" (2026-08-14).

Negative-spec: does not exercise sub-reaps (i)/(ii)/(iii) or the session.reap
op handler's cadence-gate/liveness plumbing — pure unit coverage of the new
sync helper against a tmp_path tree, matching sibling test files' fixture
idiom (no real .git/, no daemon spawn).
"""

from __future__ import annotations

import time
from pathlib import Path

from coordinator_core.ops.session import reap


def _make_archive_entry(archive_root: Path, name: str, *, age_seconds: float) -> Path:
    entry = archive_root / name
    entry.mkdir(parents=True)
    (entry / "touched.txt").write_text("x", encoding="utf-8")
    stamp = time.time() - age_seconds
    import os

    os.utime(entry, (stamp, stamp))
    return entry


def test_prunes_entries_older_than_retention_window(tmp_path):
    """An _agents-* archive entry older than the 14d retention window is
    deleted; one within the window is kept."""
    sessions_dir = tmp_path / "coordinator-sessions"
    archive_root = sessions_dir / ".archive"
    archive_root.mkdir(parents=True)

    old_entry = _make_archive_entry(
        archive_root, "_agents-old-20260101",
        age_seconds=reap._AGENT_ARCHIVE_RETENTION_SECONDS + 3600,
    )
    fresh_entry = _make_archive_entry(
        archive_root, "_agents-fresh-20260814",
        age_seconds=3600,
    )

    pruned, failed = reap._prune_stale_agent_archive(sessions_dir)

    assert pruned == ["_agents-old-20260101"]
    assert failed == []
    assert not old_entry.exists()
    assert fresh_entry.exists()


def test_never_touches_non_agents_archive_entries(tmp_path):
    """A stray non-`_agents-*` archive entry (e.g. a sub-reap (i) stale-session
    archive) is never globbed or deleted — confined strictly to the _agents-*
    prefix."""
    sessions_dir = tmp_path / "coordinator-sessions"
    archive_root = sessions_dir / ".archive"
    archive_root.mkdir(parents=True)

    session_entry = _make_archive_entry(
        archive_root, "sess-abc-2026-01-01",
        age_seconds=reap._AGENT_ARCHIVE_RETENTION_SECONDS + 3600,
    )

    pruned, failed = reap._prune_stale_agent_archive(sessions_dir)

    assert pruned == []
    assert failed == []
    assert session_entry.exists()


def test_missing_archive_dir_is_a_noop(tmp_path):
    """No .archive/ directory at all — returns empty, no error."""
    sessions_dir = tmp_path / "coordinator-sessions"
    sessions_dir.mkdir(parents=True)

    pruned, failed = reap._prune_stale_agent_archive(sessions_dir)

    assert pruned == []
    assert failed == []


def test_stat_failure_is_fail_open_skip_not_raise(tmp_path, monkeypatch):
    """An OSError raised while stat'ing one entry is logged and skipped —
    never aborts the sweep and never raises into the caller."""
    sessions_dir = tmp_path / "coordinator-sessions"
    archive_root = sessions_dir / ".archive"
    archive_root.mkdir(parents=True)

    entry = _make_archive_entry(
        archive_root, "_agents-broken-20260101",
        age_seconds=reap._AGENT_ARCHIVE_RETENTION_SECONDS + 3600,
    )

    real_stat = Path.stat

    def _boom_stat(self, *args, **kwargs):
        if self.name == "_agents-broken-20260101":
            raise OSError("simulated stat failure")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _boom_stat)

    pruned, failed = reap._prune_stale_agent_archive(sessions_dir)

    monkeypatch.undo()  # restore real Path.stat before the assertion's own .exists() call

    assert pruned == []
    assert len(failed) == 1
    assert failed[0]["id"] == "_agents-broken-20260101"
    assert entry.exists()  # never removed — fail-open keeps the entry


def test_rmtree_failure_is_fail_open_skip_not_raise(tmp_path, monkeypatch):
    """An OSError raised by shutil.rmtree for one entry is logged and
    skipped — never aborts the sweep and never raises into the caller."""
    sessions_dir = tmp_path / "coordinator-sessions"
    archive_root = sessions_dir / ".archive"
    archive_root.mkdir(parents=True)

    broken_entry = _make_archive_entry(
        archive_root, "_agents-rmbroken-20260101",
        age_seconds=reap._AGENT_ARCHIVE_RETENTION_SECONDS + 3600,
    )
    ok_entry = _make_archive_entry(
        archive_root, "_agents-rmok-20260101",
        age_seconds=reap._AGENT_ARCHIVE_RETENTION_SECONDS + 3600,
    )

    import shutil

    _orig_rmtree = shutil.rmtree

    def _boom_rmtree(path, *args, **kwargs):
        if Path(path).name == "_agents-rmbroken-20260101":
            raise OSError("simulated rmtree failure")
        return _orig_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(reap.shutil, "rmtree", _boom_rmtree)

    pruned, failed = reap._prune_stale_agent_archive(sessions_dir)

    assert pruned == ["_agents-rmok-20260101"]
    assert len(failed) == 1
    assert failed[0]["id"] == "_agents-rmbroken-20260101"
    assert "rm failed" in failed[0]["reason"]
    assert broken_entry.exists()  # never removed — fail-open keeps the entry
    assert not ok_entry.exists()
