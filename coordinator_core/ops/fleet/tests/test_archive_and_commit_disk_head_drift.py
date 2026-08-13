"""
coordinator_core.ops.fleet.tests.test_archive_and_commit_disk_head_drift

Reproduces the b3e61bd00 archive-gate bypass: a terminality predicate (e.g.
fleet.archive_shipped_handoffs._is_shipped_terminal) reads a candidate's
CURRENT ON-DISK frontmatter, but archive_and_commit's `git mv` re-keys
whatever blob the read-tree-HEAD-seeded PRIVATE INDEX holds for src (see
_common.archive_and_commit's "op-authored pre-move content" docstring
note) -- not the disk content just verified. When a candidate's on-disk
frontmatter has drifted from HEAD (an uncommitted stamp write, e.g. a
partial handoff.ship_and_archive "graceful partial outcome" leaving
deployment_state:shipped uncommitted, later joined by a separately-stamped
shipped_in also left uncommitted) a caller with restage_src=False (the
default -- both the standalone fleet.archive_shipped_handoffs op and
session.boot_sweep's batch sweep use it) can pass its disk-based
terminality re-verify while `git mv` silently commits the STALE HEAD
content -- an in_flight, no-shipped_in record -- into archive/handoffs/.

This module spawns real git (mirrors the governed model in
coordinator_core/ops/ceremony/tests/fixtures/real_git.py -- index/worktree
divergence cannot be exhibited by a mocked git, which has no index to
diverge from) but is deliberately its OWN small, scoped module (not an
import of that fixture, which is reserved for the commit-mechanism
selector's own tests) so this real-git usage stays visible in the diff,
per the standing test-cull ruling (state/audits/2026-08-07-spawn-heavy-
test-excision-ledger.md) that real-git fixtures must not go ambient again.
Exactly ONE throwaway repo, ONE test.

Spec backlink: archive-gate bypass investigation, commit b3e61bd00
("fleet: archive 7 shipped handoff(s)", Session-Id
1e7c7cb6-206d-4e07-901f-d9160018b233).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from coordinator_core.ops.fleet._common import Move, archive_and_commit


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git spawn, mirrors the
    # governed real_git.py fixture's own unguarded pattern; no console window
    # risk on the CI/dev platforms this suite runs on.
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _run(coro):
    return asyncio.run(coro)


def test_drifted_src_is_not_silently_archived_with_stale_head_content(tmp_path: Path):
    """A candidate whose on-disk content diverges from HEAD (uncommitted
    stamp) must NOT be moved via the HEAD-seeded private index -- that would
    commit stale (pre-stamp) content to the archive destination while the
    caller's own terminality re-verify read (and trusted) the fresh disk
    content. archive_and_commit must refuse the move instead."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)

    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    src = handoffs / "2026-08-06-roadmap-sedge-03.md"

    # Committed (HEAD) content: in_flight, no shipped_in -- exactly the
    # shape the 7 mis-archived records had at HEAD before b3e61bd00.
    head_content = (
        "---\n"
        "status: claimed\n"
        "deployment_state: in_flight\n"
        "---\n\nBody.\n"
    )
    src.write_text(head_content, encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: in_flight handoff"], root)
    head_sha = _git(["rev-parse", "HEAD"], root).stdout.strip()

    # Uncommitted disk-only stamp: deployment_state:shipped + a resolvable
    # shipped_in (HEAD's own SHA, so _shipped_in_resolvable-style checks
    # would pass) -- never staged, never committed. This is the drift the
    # terminality predicate sees but the private index (HEAD-seeded) does not.
    drifted_content = (
        "---\n"
        "status: claimed\n"
        f"deployment_state: shipped\nshipped_in: {head_sha}\n"
        "---\n\nBody.\n"
    )
    src.write_text(drifted_content, encoding="utf-8")

    assert src.read_text(encoding="utf-8") != head_content, "fixture sanity: disk must differ from HEAD"

    dst = root / "archive" / "handoffs" / "2026-08" / src.name
    move = Move(src=src, dst=dst, candidate_id="state/handoffs/2026-08-06-roadmap-sedge-03.md")

    acted, failed = _run(archive_and_commit(worktree_root=root, moves=[move], subject="fleet: archive 1 shipped handoff(s)"))

    # The fix: a src/HEAD content mismatch must be refused, not silently
    # archived with the stale HEAD content. Before the fix this move landed
    # in acted[] and the archived file at dst carried head_content (in_flight,
    # no shipped_in) even though disk (and the terminality check that
    # qualified this candidate) said "shipped".
    assert acted == [], f"drifted move must not be acted on: {acted}"
    assert len(failed) == 1
    assert failed[0]["id"] == move.candidate_id
    assert "drift" in failed[0]["reason"].lower() or "uncommitted" in failed[0]["reason"].lower()

    # Source is untouched; nothing was archived with stale content.
    assert src.exists()
    assert src.read_text(encoding="utf-8") == drifted_content
    assert not dst.exists()
