"""
coordinator_core.ops.fleet.tests.test_head_race_between_read_tree_and_commit

Reproduces the HEAD-race class confirmed live 6 times between 2026-08-18 and
2026-08-20 (`b4f0bfe88`, `bf51371d5`, `72c721f5f`, `c1b8e06dc`, `4e0690177`,
`1f5781ea6`): `archive_and_commit`/`rm_and_commit` seed a private index with
`git read-tree HEAD` at function entry, then land the batch's commit at the
end -- and, before this fix, nothing bridged the two. A peer commit landing
in the drift-check/os.replace/stage window resolved as the archival commit's
parent via LIVE HEAD at commit time, silently reverting the peer's work: the
resulting tree is stale-HEAD-plus-renames while the parent pointer is the
peer's own commit. `b4f0bfe88` reverted its own parent `3cb236162`,
destroying two research records permanently.

CLASS-level, per this module's own dispatch brief: a prior fix landed once
against a single call site and the class re-armed at a second one (mirrors
`test_pathspec_less_commit_seams_are_guarded.py`'s own preamble on point
fixes vs. class coverage) -- both `archive_and_commit` and `rm_and_commit`
are covered here, one test each, against the SAME race shape.

Mechanism: `asyncio.create_subprocess_exec` is monkeypatched to land a peer
commit into the throwaway repo the instant the function under test issues
its own first post-`read-tree` git spawn (`git write-tree`) -- i.e. strictly
AFTER `old_head` was captured, strictly BEFORE the commit is landed. This is
exactly the window the incident's own timeline occupies (drift
check/os.replace/stage all sit between read-tree and commit; write-tree is
the first call point downstream of ALL of them, so intercepting there
exercises the full window, not merely the narrowest slice of it).

Must FAIL against the pre-fix `git commit` (bare, no CAS) and PASS against
the write-tree + commit-tree -p <old_head> + 4-arg `update-ref` CAS.

Verification (2026-08-25, both directions, both call sites, against a
`git archive` extraction of `8350b8fa0^` -- the run report the original
docstring pointed at never existed; the session that wrote this module died
before reporting): pre-fix, archive_and_commit lands
`fleet: archive 1` with the peer commit as parent and the peer's file absent
from the committed tree, and rm_and_commit does the same; post-fix, both
refuse with `compare-and-swap failed`, fully reverse their disk moves, and
leave the peer's commit at HEAD. See `_seed_unrelated_tracked_file` for the
fixture condition the pre-fix red depends on.

Negative-spec: does NOT assert archive_and_commit/rm_and_commit's OWN
candidate succeeds under the race -- either outcome (refusal, or landing
without reverting the peer) is acceptable per the dispatch brief; the one
outcome that is NEVER acceptable is the peer's commit disappearing from
history.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet._common import Move, archive_and_commit, rm_and_commit

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git spawn, mirrors the
    # governed real_git.py fixture's own unguarded pattern; no console window
    # risk on the CI/dev platforms this suite runs on.
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _run(coro):
    return asyncio.run(coro)


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True)
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)


def _seed_unrelated_tracked_file(root: Path) -> None:
    """Seeds ONE tracked file the call under test never touches.

    Load-bearing, not scenery. Without it the repo's only tracked file is
    the candidate itself, so `rm_and_commit`'s `git rm` empties the private
    index and `_empty_private_index_breach` refuses BEFORE the commit ladder
    is reached at all — the test then passes against pre-fix and post-fix
    code alike, proving nothing about the race it names. Measured 2026-08-25
    against `8350b8fa0^`: sole-candidate seed → green pre-fix (vacuous);
    with this file → red pre-fix (peer commit reverted), green post-fix (CAS
    refuses), at both call sites.
    """
    (root / "keep.md").write_text("unrelated tracked file\n", encoding="utf-8")


def _land_peer_commit(root: Path) -> str:
    """Simulates a concurrent session's own commit landing on the SAME
    branch, in the window between `read_tree HEAD` and this call's own
    commit. Returns the peer file's repo-relative path -- the caller checks
    for its survival in HEAD's tree after the function under test returns."""
    peer_file = root / "peer-landed-while-we-were-committing.md"
    peer_file.write_text("peer session's own concurrent work\n", encoding="utf-8")
    _git(["add", "--", peer_file.name], root)
    _git(["commit", "-q", "-m", "peer: concurrent commit landing mid-archival"], root)
    return peer_file.name


def _patch_write_tree_lands_peer_commit(monkeypatch, root: Path) -> None:
    """Intercepts the FIRST `git write-tree` spawn `archive_and_commit`/
    `rm_and_commit` issue after `read-tree HEAD` (i.e. strictly after
    `old_head` is captured) and lands a peer commit just before letting it
    run for real -- landing the peer's commit exactly inside the window this
    fix closes, once per test (idempotent guard so a caller's OWN later
    `write-tree`-argv git call, if any, is not re-intercepted)."""
    orig_exec = asyncio.create_subprocess_exec
    landed = {"done": False}

    async def _intercepting_exec(*args, **kwargs):
        if not landed["done"] and len(args) >= 2 and args[0] == "git" and args[1] == "write-tree":
            landed["done"] = True
            _land_peer_commit(root)
        return await orig_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _intercepting_exec)


def test_archive_and_commit_never_reverts_a_peer_commit_landed_mid_race(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _init_repo(root)

    src = root / "state" / "handoffs" / "candidate.md"
    src.parent.mkdir(parents=True)
    src.write_text("candidate content\n", encoding="utf-8")
    _seed_unrelated_tracked_file(root)
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: candidate handoff"], root)

    _patch_write_tree_lands_peer_commit(monkeypatch, root)

    dst = root / "archive" / "handoffs" / "candidate.md"
    move = Move(src=src, dst=dst, candidate_id="state/handoffs/candidate.md", restage_src=True)

    acted, failed = _run(
        archive_and_commit(worktree_root=root, moves=[move], subject="fleet: archive 1 candidate")
    )

    log = _git(["log", "--format=%H"], root).stdout.strip().splitlines()
    assert len(log) >= 2, "peer commit must still be reachable from HEAD (or its own ref) — history was not erased"

    # The one unacceptable outcome: the peer's own commit is gone from
    # history, or its content is missing from HEAD's tree — the silent
    # revert this fix exists to close. Either refusal (failed[] carries the
    # candidate) or a successful landing that PRESERVES the peer's file is
    # acceptable.
    # check=False: a path missing from HEAD exits 128, and letting that raise
    # would replace this test's own diagnostic assertion with a bare
    # CalledProcessError — the silent-revert signal reported as plumbing noise.
    show = _git(
        ["show", "HEAD:peer-landed-while-we-were-committing.md"], root, check=False
    )
    assert "peer session's own concurrent work" in show.stdout, (
        "peer commit was silently reverted — the exact HEAD-race defect "
        f"this test exists to catch. acted={acted} failed={failed}"
    )


def test_rm_and_commit_never_reverts_a_peer_commit_landed_mid_race(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _init_repo(root)

    target = root / "state" / "review-trail" / "candidate.md"
    target.parent.mkdir(parents=True)
    target.write_text("candidate content\n", encoding="utf-8")
    _seed_unrelated_tracked_file(root)
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: candidate review-trail entry"], root)

    _patch_write_tree_lands_peer_commit(monkeypatch, root)

    reaped, failed = _run(
        rm_and_commit(worktree_root=root, paths=[target], subject="fleet: reap 1 candidate")
    )

    log = _git(["log", "--format=%H"], root).stdout.strip().splitlines()
    assert len(log) >= 2, "peer commit must still be reachable from HEAD (or its own ref) — history was not erased"

    # check=False: a path missing from HEAD exits 128, and letting that raise
    # would replace this test's own diagnostic assertion with a bare
    # CalledProcessError — the silent-revert signal reported as plumbing noise.
    show = _git(
        ["show", "HEAD:peer-landed-while-we-were-committing.md"], root, check=False
    )
    assert "peer session's own concurrent work" in show.stdout, (
        "peer commit was silently reverted — the exact HEAD-race defect "
        f"this test exists to catch. reaped={reaped} failed={failed}"
    )
