"""
coordinator_core.ops.fleet.tests.test_archive_git_free_seam_smoke

Proves archive_git_free_seam.patched_disposition_seam generalises
test_archive_dest_conflict_wedge_detector.py's by-hand three-name patch
(check_repo_root / main_worktree_root / archive_and_commit) without
changing the disposition asserted: prune_bugs's real production _handler,
git-free, dest-conflict case. This is the seam ~20 archive-family modules
are meant to import — this test is the first (and simplest) proof it does
what its docstring claims, kept in its own file so a future edit to the
seam has an immediate, minimal regression signal.

Not a replacement for test_archive_dest_conflict_wedge_detector.py (which
is untouched per AC9/Anti-scope) — a second, independent exercise of the
same disposition through the generalised helper instead of the hand-
rolled patch set.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.fleet import prune_bugs
from coordinator_core.ops.fleet._common import _REASON_DEST_CONFLICT
from coordinator_core.ops.fleet.tests.archive_git_free_seam import (
    patched_disposition_seam,
    run,
)


def _make_bug(worktree: Path, name: str, *, status: str = "closed") -> Path:
    bug_dir = worktree / "state" / "bug-backlog"
    bug_dir.mkdir(parents=True, exist_ok=True)
    path = bug_dir / name
    path.write_text(f"status: {status}\ntitle: {name}\n", encoding="utf-8")
    return path


def test_seam_reproduces_dest_conflict_disposition_git_free(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/bug-backlog/2026-08-01-wedged.yaml"
    src = _make_bug(worktree, "2026-08-01-wedged.yaml")

    dst_dir = worktree / "archive" / "bug-backlog" / "2026-08"
    dst_dir.mkdir(parents=True)
    dst = dst_dir / "2026-08-01-wedged.yaml"
    dst.write_text("status: closed\ntitle: a DIFFERENT archived copy\n", encoding="utf-8")

    with patched_disposition_seam(prune_bugs, worktree=worktree) as mover:
        result = run(prune_bugs._handler(
            {"mode": "already-terminal", "dry_run": False, "candidate_ids": [cid]},
            repo_root=str(worktree),
        ))

    assert result["skipped"] == [{"id": cid, "reason": _REASON_DEST_CONFLICT}]
    assert result["acted"] == []
    assert mover.captured is None, (
        "a genuine conflict must never reach the mover through the "
        "generalised seam either"
    )
    assert src.exists()
    assert dst.read_text(encoding="utf-8") == "status: closed\ntitle: a DIFFERENT archived copy\n"


def test_seam_reproduces_byte_identical_convergence_git_free(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/bug-backlog/2026-08-01-dup.yaml"
    body = "status: closed\ntitle: 2026-08-01-dup.yaml\n"
    src = _make_bug(worktree, "2026-08-01-dup.yaml")
    src.write_text(body, encoding="utf-8")

    dst_dir = worktree / "archive" / "bug-backlog" / "2026-08"
    dst_dir.mkdir(parents=True)
    dst = dst_dir / "2026-08-01-dup.yaml"
    dst.write_text(body, encoding="utf-8")

    with patched_disposition_seam(prune_bugs, worktree=worktree) as mover:
        result = run(prune_bugs._handler(
            {"mode": "already-terminal", "dry_run": False, "candidate_ids": [cid]},
            repo_root=str(worktree),
        ))

    assert result["skipped"] == []
    assert result["acted"] == [{"id": cid, "archived": True}]
    assert mover.captured is not None
    assert len(mover.captured) == 1
    move = mover.captured[0]
    assert move.candidate_id == cid
    assert move.force is True
