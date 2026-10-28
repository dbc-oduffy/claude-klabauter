"""Lock-contended post-landing index resync exhaustion is advisory state, never a bug row."""

import asyncio
import json
from pathlib import Path

from coordinator_core.ops.fleet._common import (
    Move,
    _persist_index_resync_failure,
    _resync_main_index_for_moves,
    _resync_main_index_for_reaps,
)
from coordinator_core.ops.fleet._index_resync_pending import (
    ADVISORY_DIRNAME,
    list_pending,
)

LOCK = "fatal: Unable to create '.git/index.lock': File exists."
NON_LOCK = "fatal: unable to write new index file"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _persist(root, reason):
    _persist_index_resync_failure(
        worktree_root=root, candidate_id="a.md", reason=reason, op_label="archive_and_commit",
    )


def _advisories(root):
    d = root / ".git" / ADVISORY_DIRNAME
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))] if d.is_dir() else []


def test_lock_contention_files_no_bug_row_and_leaves_advisory(tmp_path):
    root = _repo(tmp_path)
    _persist(root, f"restore-staged-failed: {LOCK}")
    assert not (root / "state" / "bug-backlog").exists()
    (rec,) = _advisories(root)
    assert rec["candidate_id"] == "a.md"
    assert rec["index_lock_present_at_record"] is False


def test_advisory_reports_live_lock_only_when_lock_file_exists(tmp_path):
    root = _repo(tmp_path)
    (root / ".git" / "index.lock").write_text("")
    _persist(root, LOCK)
    assert _advisories(root)[0]["index_lock_present_at_record"] is True


def test_non_lock_failure_still_files_bug_row(tmp_path):
    root = _repo(tmp_path)
    _persist(root, NON_LOCK)
    assert len(list((root / "state" / "bug-backlog").glob("*.yaml"))) == 1
    assert _advisories(root) == []


class _Fake:
    def __init__(self, err):
        self.err = err

    async def __call__(self, argv, *, cwd, env):
        return self.err


def test_moves_resync_exhaustion_on_lock_keeps_pending_and_no_row(tmp_path):
    root = _repo(tmp_path)
    move = Move(src=root / "src.md", dst=root / "dst.md", candidate_id="src.md")
    acted = {"src.md": {"id": "src.md", "archived": True}}
    asyncio.run(
        _resync_main_index_for_moves([move], acted, worktree_root=root, env={}, run_git=_Fake(LOCK))
    )
    assert "index_resync_failed" in acted["src.md"]
    assert not (root / "state" / "bug-backlog").exists()
    assert len(list_pending(root)) == 1
    assert len(_advisories(root)) == 1


def test_reaps_resync_exhaustion_on_lock_files_no_row(tmp_path):
    root = _repo(tmp_path)
    path = root / "gone.md"
    reaped = {"gone.md": {"id": "gone.md"}}
    asyncio.run(
        _resync_main_index_for_reaps([path], reaped, worktree_root=root, env={}, run_git=_Fake(LOCK))
    )
    assert "index_resync_failed" in reaped["gone.md"]
    assert not (root / "state" / "bug-backlog").exists()
    assert len(_advisories(root)) == 1
