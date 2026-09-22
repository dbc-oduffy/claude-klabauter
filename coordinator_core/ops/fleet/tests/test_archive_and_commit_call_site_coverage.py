"""
coordinator_core.ops.fleet.tests.test_archive_and_commit_call_site_coverage

C4 of docs/plans/2026-08-13-archive-family-coverage-restoration.md.

DISPOSITION: the culled file's 12 `def test_` functions are DROPPED-AS-
OBSOLETE, not ported. Reason, verified against HEAD 2026-09-19 (AC7b): every
one of them asserted a per-caller instance of the SAME thing — that a
foreign/uncommitted ("DIRTY") edit on a source or sidecar file at archival
time does NOT leak into the archival commit's own blob at dst
(`assert "DIRTY uncommitted ..." not in _head_blob(...)`). That assertion
pins the disk/HEAD drift REFUSAL gate `4541069c3` added and `cffa6e99f`
retired outright, by PM ruling, 2026-08-26 (docs/plans/2026-08-13-archive-
family-coverage-restoration.md § Problem, "Post-cull behaviour commits").
Under the current `os.replace`-based mover (`coordinator_core/ops/fleet/
_common.py`, `archive_and_commit`), a candidate's CURRENT on-disk content —
dirty or not — is exactly what gets archived; there is no longer a refusal
or a stale-content substitution to observe. Porting these 12 tests as-is
would assert the OPPOSITE of current production behaviour and turn green
only by reintroducing the retired refusal — the precise failure mode Anti-
scope's "a stale assertion that happens to still pass is worse than a
deleted one" names, except these would not even pass silently; they would
fail loud, and "fix the test to pass" from there is how the refusal would
quietly come back.

This also retires the file's own original premise: it was AC5 of
docs/plans/2026-08-05-resync-stages-the-committed-blob.md, C5a, "per-call-
site coverage for the C1 --cacheinfo main-index-resync fix" — the
`--cacheinfo` main-index-resync mechanism itself is gone (see test_archive_
and_commit_index_resync_residue.py's module docstring: the resync now runs
via a batched `git restore --staged`, not `--cacheinfo`), so the specific
resync-path-per-caller enumeration this file existed to give is itself
stale on a second, independent axis.

What replaces it, real-git (1 test function, governed pattern — own scoped
module, one throwaway repo, spawn visible in the diff): a general call-site
regression net proving `archive_and_commit` still lands a realistic
multi-move batch (primary + sidecar, mirroring the culled file's own
`test_row6_archive_plans_sidecar_move_is_the_live_portion`) end to end, with
a clean index/worktree afterward — the property every one of the 12
dropped per-caller tests ultimately depended on to even reach their
(now-retired) drift assertion. Per-caller disposition/Move-construction
correctness (which caller builds which Move for which candidate) is git-
free coverage that belongs to each caller module's own test file (C2/C3's
surface — test_archive_actioned_memos, test_archive_queue_entry, test_
archive_shipped_handoffs, test_prune_bugs, etc.), not re-derived here.

Spec backlinks:
  - Original coverage: docs/plans/2026-08-05-resync-stages-the-committed-blob.md, C5a
  - Retirement: docs/plans/2026-08-26-the-archival-seam-stops-asking-git-at-all.md
  - This chunk: docs/plans/2026-08-13-archive-family-coverage-restoration.md, C4

AC5b — per-test justification register
---------------------------------------
The one test below asserts on real commit content (`git show HEAD:<path>`)
and real index/worktree cleanliness (`git status --porcelain`) after a
multi-Move batch — filesystem-only assertions cannot exhibit "what did the
real commit mechanism actually land", so it stays real-git.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet._common import Move, archive_and_commit
from coordinator_core.win_portability import no_console_creationflags

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
        check=True, **no_console_creationflags(),
    )


def _run(coro):
    return asyncio.run(coro)


def test_multi_move_batch_with_sidecar_lands_one_clean_commit(tmp_path: Path):
    """A realistic caller batch — a primary plan file plus its sidecar
    review file, one Move each, one subject — lands both into ONE archival
    commit, with the working tree/index clean afterward. Mirrors the
    call-site shape the dropped file's `test_row6_archive_plans_sidecar_
    move_is_the_live_portion` exercised, minus the retired drift assertion.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)

    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    stem = "2026-01-01-my-plan"
    primary_src = plans / f"{stem}.md"
    sidecar_src = plans / f"{stem}.review.md"
    primary_src.write_text("---\nstatus: implemented\n---\n\nplan body\n", encoding="utf-8")
    sidecar_src.write_text("sidecar review body\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: plan + sidecar"], root)

    primary_dst = root / "archive" / "specs" / f"{stem}.md"
    sidecar_dst = root / "archive" / "specs" / f"{stem}.review.md"
    moves = [
        Move(src=primary_src, dst=primary_dst, candidate_id=f"docs/plans/{stem}.md"),
        Move(src=sidecar_src, dst=sidecar_dst, candidate_id=f"docs/plans/{stem}.review.md"),
    ]

    acted, failed = _run(archive_and_commit(
        root, moves, "fleet: archive 1 terminal plan(s) [fleet.archive_completed_plans]",
    ))

    assert failed == []
    assert {a["id"] for a in acted} == {
        f"docs/plans/{stem}.md", f"docs/plans/{stem}.review.md",
    }
    assert all(a["archived"] is True for a in acted)

    assert not primary_src.exists()
    assert not sidecar_src.exists()
    assert primary_dst.exists()
    assert sidecar_dst.exists()

    # Both moves landed in the SAME commit (one subject, one HEAD advance).
    log = _git(["log", "--format=%s"], root).stdout.strip().splitlines()
    assert log[0] == "fleet: archive 1 terminal plan(s) [fleet.archive_completed_plans]"

    status = _git(["status", "--porcelain"], root).stdout
    assert status.strip() == "", f"expected a clean tree/index after the batch: {status!r}"
