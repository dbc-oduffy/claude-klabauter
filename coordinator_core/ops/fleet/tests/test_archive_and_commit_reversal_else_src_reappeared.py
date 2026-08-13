"""
coordinator_core.ops.fleet.tests.test_archive_and_commit_reversal_else_src_reappeared

AC4 of docs/plans/2026-08-13-fleet-archive-dest-collision-vs-idempotent-
replay.md: both post-`git mv`-failure reversal guards in `archive_and_commit`
carry an `elif move.dst.exists():` alongside their original
`if move.dst.exists() and not move.src.exists():` — deliberately an `elif`
narrowing, not a bare `else`. When `git mv` fails because a destination
collision means src never left in the first place (the same disk shape a
transient reappearance produces: dst present, src also present), the
reversal cannot run without clobbering dst, so it must WARN and land the
orphaned dst in `failed[]` at creation time rather than silently no-op. When
`git mv` fails BEFORE touching anything — dst never came into existence at
all (e.g. a bad source pathspec) — the `elif`'s condition is false and the
plain-failure path must stay quiet: no WARNING, no "reversal-skipped"
annotation.

Spawns real git — index/worktree divergence (a destination that already
exists in HEAD, versus a src pathspec that git refuses before touching disk)
cannot be exhibited by a mocked git, which has no index to diverge from; same
argument test_archive_and_commit_disk_head_drift.py's docstring makes for its
own existence. Deliberately its OWN small, scoped module (not an import of
coordinator_core/ops/ceremony/tests/fixtures/real_git.py, which is reserved
for the commit-mechanism selector's own tests) so this real-git usage stays
visible in the diff, per the standing test-cull ruling
(state/audits/2026-08-07-spawn-heavy-test-excision-ledger.md) that real-git
fixtures must not go ambient again. Exactly ONE throwaway repo, ONE test
function (covering both the elif-fires and elif-stays-quiet shapes, since
both are necessary to pin the narrowing and neither alone would catch a
regression to a bare `else`).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet._common import Move, archive_and_commit

# Real-git spawn is load-bearing: the elif narrowing being pinned depends on
# index/worktree divergence (a destination `git mv` refuses upfront vs. a
# src pathspec it refuses before touching disk) that a mocked git has no
# index to diverge from. Single test, single repo -- no module-scope hoist
# needed. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


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


def test_reversal_elif_narrowing_pins_reappeared_case_and_quiet_plain_failure(
    tmp_path: Path, caplog,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)

    src_dir = root / "a"
    src_dir.mkdir()
    src = src_dir / "src.txt"
    src.write_text("src content\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed src"], root)

    # dst pre-exists and is tracked (real archived history) -- `git mv` (no
    # force) refuses upfront: BOTH src and dst remain on disk. This is the
    # same disk shape "src has reappeared" describes: the reversal branch's
    # `if ... and not move.src.exists()` is false because src never left,
    # and the elif must fire instead of silently no-op'ing.
    dst_dir = root / "b"
    dst_dir.mkdir()
    dst = dst_dir / "dst.txt"
    dst.write_text("real archived content\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed dst"], root)

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.fleet._common"):
        acted, failed = _run(archive_and_commit(
            worktree_root=root,
            moves=[Move(src=src, dst=dst, candidate_id="a/src.txt")],
            subject="test: reappeared-src reversal",
        ))

    assert acted == []
    assert len(failed) == 1
    assert failed[0]["id"] == "a/src.txt"
    assert "reversal-skipped-src-reappeared" in failed[0]["reason"]
    assert any(
        "skipping reversal" in rec.message and "reappeared" in rec.message
        for rec in caplog.records
    ), "the elif branch must WARN naming the reappeared-source skip"

    # Both copies survive untouched -- no clobber, no phantom move.
    assert src.exists()
    assert src.read_text(encoding="utf-8") == "src content\n"
    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == "real archived content\n"

    caplog.clear()

    # Plain-failure case, narrowing's other half: git mv fails before
    # touching ANYTHING (bad source pathspec) -- dst never comes into
    # existence, so the elif's condition is false and the reversal path
    # must stay quiet: no WARNING, no "reversal-skipped" annotation. A bare
    # `else` (the regression this pins against) would fire here too and
    # mis-warn about a dst that was never created.
    new_dst = dst_dir / "never-created.txt"
    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.fleet._common"):
        acted2, failed2 = _run(archive_and_commit(
            worktree_root=root,
            moves=[Move(
                src=src_dir / "does-not-exist.txt",
                dst=new_dst,
                candidate_id="a/does-not-exist.txt",
            )],
            subject="test: plain git-mv failure, dst never created",
        ))

    assert acted2 == []
    assert len(failed2) == 1
    assert failed2[0]["id"] == "a/does-not-exist.txt"
    assert "reversal-skipped-src-reappeared" not in failed2[0]["reason"]
    assert not new_dst.exists()
    assert not any(
        "skipping reversal" in rec.message for rec in caplog.records
    ), "the plain git-mv-failure-before-touching-anything case must stay quiet"
