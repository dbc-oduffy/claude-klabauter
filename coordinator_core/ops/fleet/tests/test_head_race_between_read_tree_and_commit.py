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

Mechanism (RE-TARGETED, 2026-08-26, C7 -- see this module's own dispatch
brief in `docs/plans/2026-08-26-the-archival-commit-helper-computes-its-own-
tree.md` C7): C2 (`dccf2fc01`) deleted `archive_and_commit`'s `git
write-tree` spawn entirely -- it now lands via `_commit_via_head_spine`,
whose own tree build is a spawn-free in-process spine read. The original
interception point (the first `git write-tree` argv after `read-tree HEAD`)
therefore NEVER FIRES on the post-C2 path: the monkeypatch's guard condition
is dead code, no peer commit is ever landed, and the peer-survival assertion
below trivially passes -- a false-positive green mistakeable for "the race is
closed", when the true state was "this path is untested for the race".

Two different interception points now, one per function, because the two
functions no longer share a spawn shape in the race window:

- `archive_and_commit`: intercepts `_hash_object_stdin_paths` (the ONE spawn
  C2 left between `old_head`'s capture and the commit landing for a
  `restage_src=True` move -- the drift-check `git diff` spawn is skipped
  entirely for such a move, see `Move.restage_src`'s docstring). This is a
  synchronous wrapper (`git_native`'s own `_git()`, `subprocess.run` under
  the hood, offloaded via `asyncio.to_thread`) -- NOT an
  `asyncio.create_subprocess_exec` call -- so it is intercepted by wrapping
  the module-level name itself, not by patching `asyncio.create_subprocess_exec`.
- `rm_and_commit`: intercepts the per-path `git rm --` spawn (still
  `asyncio.create_subprocess_exec`-based). Chosen over `write-tree` because
  C3 (`docs/plans/...archival-commit-helper...md` C3, landing concurrently
  in this same wave) removes `rm_and_commit`'s own `commit-tree`/`update-ref`
  pair but explicitly, in its own dispatch brief, keeps `git rm --` --
  "MUTATES THE WORKTREE... it stays" -- so this interception point is stable
  across both the pre-C3 and post-C3 shape of the function, unlike
  `write-tree`, which C3's re-siting of `_empty_private_index_breach` may
  remove the same way C2 removed it from `archive_and_commit`.

Both interception points sit strictly AFTER `old_head` is captured and
strictly BEFORE the commit lands, in both the current and (for
`rm_and_commit`) the anticipated post-C3 shape -- the same window the
incident's own timeline occupies.

Must FAIL against a pre-fix `git commit` (bare, no CAS) and PASS against
the CAS-bridged landing (`_commit_via_head_spine`'s locked `cas_ref`
compare-and-swap for `archive_and_commit`; the write-tree + commit-tree -p
<old_head> + 4-arg `update-ref` CAS, or its post-C3 spine equivalent, for
`rm_and_commit`).

Verification (2026-08-26, re-run against this re-targeted mechanism, HEAD at
dispatch time): both tests pass, and passing is itself proof the
interception fired rather than a vacuous green -- reasoned explicitly rather
than by weakening the CAS in `_common.py` (out of this chunk's writes, owned
by C3's concurrent executor). The fixture starts with NO peer file and NO
peer commit; the only code path that ever creates
`peer-landed-while-we-were-committing.md` or commits it is
`_land_peer_commit`, called exclusively from inside the interception
wrapper. The final assertion greps that exact file's content out of
`HEAD`'s tree. A pass is therefore only reachable if `_land_peer_commit` ran
-- i.e. the interception fired -- AND the peer's commit survived at HEAD.
Had the wrapped call site never fired (the pre-re-target false-positive this
chunk exists to close), `git show HEAD:peer-landed-while-we-were-committing.md`
would exit 128 with empty stdout and the assertion would fail, not pass
vacuously. Both functions currently land their own commit successfully
alongside the peer's (rather than refusing) in this exercise, which is
itself an acceptable outcome per this module's negative-spec -- the one
unacceptable outcome, the peer's commit disappearing from history, does not
occur either way. See `_seed_unrelated_tracked_file` for the fixture
condition the pre-fix red depends on.

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


def _patch_hash_object_lands_peer_commit(monkeypatch, root: Path) -> None:
    """`archive_and_commit`-specific interception (see module docstring's
    2026-08-26 re-target). `_hash_object_stdin_paths` is the one spawn C2
    left between `old_head`'s capture and the commit landing for a
    `restage_src=True` move; it is a synchronous wrapper (`subprocess.run`
    under the hood, offloaded to a thread), so it is intercepted by wrapping
    the module-level name `archive_and_commit` actually calls, not by
    patching `asyncio.create_subprocess_exec`."""
    import coordinator_core.ops.fleet._common as _common_mod

    orig = _common_mod._hash_object_stdin_paths

    def _intercepting(*args, **kwargs):
        _land_peer_commit(root)
        return orig(*args, **kwargs)

    monkeypatch.setattr(_common_mod, "_hash_object_stdin_paths", _intercepting)


def _patch_git_rm_lands_peer_commit(monkeypatch, root: Path) -> None:
    """`rm_and_commit`-specific interception (see module docstring's
    2026-08-26 re-target). Intercepts the FIRST per-path `git rm --` spawn --
    strictly after `old_head` is captured, strictly before the commit lands,
    and stable across both the pre-C3 and post-C3 shape of the function (C3's
    own dispatch brief keeps `git rm --` explicitly: "MUTATES THE WORKTREE...
    it stays"), unlike `write-tree`, which C3 may re-site the same way C2
    re-sited it out of `archive_and_commit`. Idempotent guard so a caller's
    own later `git rm` call, if any, is not re-intercepted."""
    orig_exec = asyncio.create_subprocess_exec
    landed = {"done": False}

    async def _intercepting_exec(*args, **kwargs):
        if not landed["done"] and len(args) >= 2 and args[0] == "git" and args[1] == "rm":
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

    _patch_hash_object_lands_peer_commit(monkeypatch, root)

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

    _patch_git_rm_lands_peer_commit(monkeypatch, root)

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
