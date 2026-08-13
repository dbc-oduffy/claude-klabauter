"""
coordinator_core.ops.fleet.tests.test_archive_dest_conflict_wedge_detector

AC8 of docs/plans/2026-08-13-fleet-archive-dest-collision-vs-idempotent-
replay.md: the wedge shape — a candidate whose basename is live in its
source directory (e.g. state/handoffs/) AND already present at the archive
destination, with DIFFERING bytes — must surface as `_REASON_DEST_CONFLICT`,
never be skipped as a converged/"already-archived" idempotent replay. The
byte-identical twin must converge (force-overwrite archive), not wedge.

Git-free by design. Two layers:

1. `_is_identical_duplicate` (coordinator_core.ops.fleet._common) is a pure
   byte comparison over two paths — reached directly with `tmp_path`, no
   mocking needed. This is "the cheapest thing that proves it" per the
   dispatch brief: the predicate IS the three-way disposition's decision
   function, so exercising it directly already proves the differ/identical
   split.

2. `fleet.prune_closed_bugs`'s `_handler` is exercised end-to-end (through
   the real production disposition code at prune_bugs.py, not a
   reimplementation of it) with `archive_and_commit` monkeypatched to a
   recording stub — the narrow "mover" seam the dispatch brief calls out —
   so the git-mv/commit mechanics never run. `prune_bugs` was chosen over
   `archive_handoffs`/`archive_shipped_handoffs` because its terminality
   check is a plain frontmatter `status: closed` read with no session-
   liveness resolution, keeping the fixture minimal while still exercising
   real, shipped disposition code (not a mock of it). The differing-bytes
   case never reaches `archive_and_commit` at all (the module `continue`s
   straight to `skipped[]`), so even the monkeypatch is exercised only by
   the converging (byte-identical) case.

No real git anywhere in this module — the disposition logic under test does
not require index/worktree divergence to observe, unlike the reversal-else
case (see test_archive_and_commit_reversal_else_src_reappeared.py), which
does and is therefore a separate, real-git module.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.fleet import prune_bugs
from coordinator_core.ops.fleet._common import _REASON_DEST_CONFLICT, _is_identical_duplicate


# ---------------------------------------------------------------------------
# Layer 1: the pure predicate, directly.
# ---------------------------------------------------------------------------

def test_is_identical_duplicate_false_when_bytes_differ(tmp_path: Path) -> None:
    src = tmp_path / "live.md"
    dst = tmp_path / "archived.md"
    src.write_text("live content\n", encoding="utf-8")
    dst.write_text("different archived content\n", encoding="utf-8")

    assert _is_identical_duplicate(src, dst) is False


def test_is_identical_duplicate_true_when_bytes_match(tmp_path: Path) -> None:
    src = tmp_path / "live.md"
    dst = tmp_path / "archived.md"
    src.write_bytes(b"identical bytes\n")
    dst.write_bytes(b"identical bytes\n")

    assert _is_identical_duplicate(src, dst) is True


# ---------------------------------------------------------------------------
# Layer 2: the real three-way disposition inside a shipped handler, mover
# monkeypatched.
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


async def _fake_archive_and_commit(worktree_root, moves, subject):
    """Records what it was asked to move; never spawns git."""
    _fake_archive_and_commit.captured = list(moves)
    acted = [{"id": m.candidate_id, "archived": True} for m in moves]
    return acted, []


def _make_bug(worktree: Path, name: str, *, status: str = "closed") -> Path:
    bug_dir = worktree / "state" / "bug-backlog"
    bug_dir.mkdir(parents=True, exist_ok=True)
    path = bug_dir / name
    path.write_text(f"status: {status}\ntitle: {name}\n", encoding="utf-8")
    return path


def test_wedge_shape_reports_dest_conflict_not_already_archived(tmp_path: Path) -> None:
    """Differing dst: reported as _REASON_DEST_CONFLICT, never as a converged skip,
    and the reversal/mover seam is never even reached — the module `continue`s
    before building a Move."""
    worktree = tmp_path / "repo"
    cid = "state/bug-backlog/2026-08-01-wedged.yaml"
    src = _make_bug(worktree, "2026-08-01-wedged.yaml")

    dst_dir = worktree / "archive" / "bug-backlog" / "2026-08"
    dst_dir.mkdir(parents=True)
    dst = dst_dir / "2026-08-01-wedged.yaml"
    dst.write_text("status: closed\ntitle: a DIFFERENT archived copy\n", encoding="utf-8")

    with patch.object(prune_bugs, "check_repo_root", lambda param_root, common_dir_arg: None), \
         patch.object(prune_bugs, "main_worktree_root", lambda common_dir: worktree), \
         patch.object(prune_bugs, "archive_and_commit", _fake_archive_and_commit):
        _fake_archive_and_commit.captured = None
        result = _run(prune_bugs._handler(
            {"mode": "already-terminal", "dry_run": False, "candidate_ids": [cid]},
            repo_root=str(worktree),
        ))

    assert result["skipped"] == [{"id": cid, "reason": _REASON_DEST_CONFLICT}]
    assert result["acted"] == []
    assert _fake_archive_and_commit.captured is None, (
        "a genuine conflict must never reach the mover — it is not idempotent "
        "replay, so no Move should have been built"
    )
    # Both copies survive untouched: the differing dst is real archived history.
    assert src.exists()
    assert dst.read_text(encoding="utf-8") == "status: closed\ntitle: a DIFFERENT archived copy\n"


def test_byte_identical_twin_converges_via_force_overwrite(tmp_path: Path) -> None:
    """Same shape, but dst is byte-identical to src: this is idempotent replay
    and must converge — a Move with force=True reaches the mover, not a skip."""
    worktree = tmp_path / "repo"
    cid = "state/bug-backlog/2026-08-01-dup.yaml"
    body = "status: closed\ntitle: 2026-08-01-dup.yaml\n"
    src = _make_bug(worktree, "2026-08-01-dup.yaml")
    src.write_text(body, encoding="utf-8")

    dst_dir = worktree / "archive" / "bug-backlog" / "2026-08"
    dst_dir.mkdir(parents=True)
    dst = dst_dir / "2026-08-01-dup.yaml"
    dst.write_text(body, encoding="utf-8")

    with patch.object(prune_bugs, "check_repo_root", lambda param_root, common_dir_arg: None), \
         patch.object(prune_bugs, "main_worktree_root", lambda common_dir: worktree), \
         patch.object(prune_bugs, "archive_and_commit", _fake_archive_and_commit):
        _fake_archive_and_commit.captured = None
        result = _run(prune_bugs._handler(
            {"mode": "already-terminal", "dry_run": False, "candidate_ids": [cid]},
            repo_root=str(worktree),
        ))

    assert result["skipped"] == []
    assert result["acted"] == [{"id": cid, "archived": True}]
    assert _fake_archive_and_commit.captured is not None
    assert len(_fake_archive_and_commit.captured) == 1
    move = _fake_archive_and_commit.captured[0]
    assert move.candidate_id == cid
    assert move.force is True, "byte-identical convergence must force-overwrite, not skip"
