"""
coordinator_core.ops.fleet.tests.test_common_rm_and_commit

Unit tests for coordinator_core.ops.fleet._common.rm_and_commit — the async
git-rm SIBLING of archive_and_commit (delete semantic, DR-211 D3/D4 mechanics).

Coverage:
  - happy path: git rm + scoped commit removes tracked files, history preserved.
  - private-index isolation: rm_and_commit does not absorb/clobber a concurrent
    main-index staging (mirrors test_fleet_common.py's isolation proof for the
    rename sibling).
  - fail-closed on worktree-modified: plain `git rm` (never -f) refuses to stage
    a deletion when the worktree file differs from HEAD; the id lands in
    failed[] and the file is NOT deleted.
  - AC7 (rm_and_commit's actual FORWARD-B exposure): a swept batch where ONE
    path carries a foreign dirty edit and a SIBLING path is clean — the dirty
    path's `git rm` refuses (fail-closed, per the bullet above) and its edit
    survives untouched; the clean sibling is still reaped and committed, and
    the commit (now sourced purely from the private index with no trailing
    pathspec, see C4) carries no trace of the dirty file. Unlike
    archive_and_commit, rm_and_commit was never actually exploitable by
    FORWARD-B in practice — `git rm` (never -f) blocks the reap before the
    commit call is ever reached — so this test asserts the fail-closed-blocks
    behaviour that genuinely holds here, not a race-injected absorption.
  - AC7 mechanism pin (docs/plans/2026-08-05-resync-stages-the-committed-blob.md
    C6): the tests above pin BEHAVIOUR (dirty file survives, clean sibling is
    reaped). `test_pins_git_argv_remove_only_no_pathspec_commit_never_force`
    additionally pins the actual subprocess argv rm_and_commit invokes —
    observed via a recording monkeypatch on asyncio.create_subprocess_exec,
    not assumed from the docstring — confirming (1) every `git rm` call is
    the plain `git rm -- <path>` form, never `-f`; (2) the single `git commit`
    call carries no trailing worktree pathspec (index-sourced, no worktree
    read at commit time); and (3) every `git update-index` resync call uses
    `--remove` and never `--add` (the reaped file is gone; there is nothing
    to re-add). This is the disk-truth check for rm_and_commit's own
    NEGATIVE-SPEC claims, run against a genuinely dirty path per the plan's
    "assert it against a dirty path — do not assume it" instruction.
  - commit-failure reversal: on commit failure every git-rm'd path is restored
    from HEAD (`git checkout HEAD -- <path>`), NEVER a rename-back (there is no
    dst — the unit of work is a delete).
  - idempotent no-op: rm_and_commit(worktree, [], subject) -> ([], []), no
    read-tree, no commit.

Spec backlinks:
  - Plan (C4): docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md (D3/D4,
    FORWARD-B amendment)
  - DR-218: docs/decisions/DR-218-review-trail-aged-unintegrated-reap-boundary.md
  - Pattern mirrors: test_archive_shipped_handoffs.py, test_fleet_common.py
    (temp-git-repo fixture style via the shared fleet_repo conftest fixture).
"""

from __future__ import annotations

import asyncio
import subprocess

from coordinator_core.ops.fleet._common import rm_and_commit


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _head_sha(fleet_repo) -> str:
    """Return the current HEAD commit SHA from fleet_repo."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


def _write_hook_that_fails(fleet_repo) -> None:
    """Install a pre-commit hook that always exits non-zero — forces `git commit`
    invoked by rm_and_commit to fail deterministically, without touching gpgsign
    (mirrors the GAP-6 gpgsign-override test's "force a git failure" technique,
    but at the commit-hook layer instead of the signing layer)."""
    hooks_dir = fleet_repo.root / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-commit"
    hook_path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook_path.chmod(0o755)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_happy_path_removes_tracked_file_and_preserves_history(fleet_repo):
    """git rm + scoped commit removes tracked files; history is preserved."""
    target = fleet_repo.root / "state" / "bug-backlog" / "2026-04-01-happy.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("title: happy\nstatus: closed\n", encoding="utf-8")
    fleet_repo._git("add", str(target))
    fleet_repo._git("commit", "-m", "add happy target")

    cid = "state/bug-backlog/2026-04-01-happy.yaml"

    reaped, failed = _run(rm_and_commit(fleet_repo.root, [target], "reap: happy path"))

    assert reaped == [{"id": cid, "reaped": True}]
    assert failed == []
    assert not target.exists(), "reaped file must be removed from disk"
    assert fleet_repo.git_status_clean()

    log = subprocess.run(
        ["git", "log", "--oneline", "--", cid],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    ).stdout.decode()
    assert log.strip() != "", (
        f"git log must find the reaped path {cid!r} (history-preserving); got empty log"
    )


# ---------------------------------------------------------------------------
# private-index isolation
# ---------------------------------------------------------------------------


def test_private_index_isolation_main_index_untouched(fleet_repo):
    """An UNRELATED file staged in the MAIN index before rm_and_commit is not
    included in the reap commit and survives staged afterward."""
    target = fleet_repo.root / "state" / "bug-backlog" / "2026-04-01-isolation-target.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("title: target\nstatus: closed\n", encoding="utf-8")
    fleet_repo._git("add", str(target))
    fleet_repo._git("commit", "-m", "add isolation target")

    sentinel = fleet_repo.root / "state" / "bug-backlog" / "sentinel-unrelated.yaml"
    sentinel.write_text("title: sentinel\nstatus: open\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", str(sentinel)],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    )

    status_before = fleet_repo._git_unchecked("status", "--porcelain").stdout.decode()
    assert "sentinel-unrelated.yaml" in status_before, (
        "sentinel must be staged in the main index before rm_and_commit"
    )

    cid = "state/bug-backlog/2026-04-01-isolation-target.yaml"
    reaped, failed = _run(rm_and_commit(fleet_repo.root, [target], "reap: private-index isolation"))

    assert reaped == [{"id": cid, "reaped": True}]
    assert failed == []

    status_after = fleet_repo._git_unchecked("status", "--porcelain").stdout.decode()
    assert "sentinel-unrelated.yaml" in status_after, (
        "sentinel staged in the main index must remain staged — rm_and_commit's "
        "private GIT_INDEX_FILE must not have interfered with the main index"
    )

    log_names = subprocess.run(
        ["git", "log", "-1", "--no-renames", "--name-only", "--format="],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    ).stdout.decode().strip().splitlines()
    assert cid in log_names
    assert "state/bug-backlog/sentinel-unrelated.yaml" not in log_names, (
        "sentinel staged only in the main index must NOT appear in the reap commit"
    )


# ---------------------------------------------------------------------------
# fail-closed on worktree-modified
# ---------------------------------------------------------------------------


def test_fail_closed_worktree_modified_refuses_delete(fleet_repo):
    """Plain `git rm` (never -f) refuses when the worktree file differs from
    HEAD; the id lands in failed[] and the file is NOT deleted."""
    target = fleet_repo.root / "state" / "bug-backlog" / "2026-04-01-modified.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    original = "title: orig\nstatus: closed\n"
    target.write_text(original, encoding="utf-8")
    fleet_repo._git("add", str(target))
    fleet_repo._git("commit", "-m", "add modified-source target")

    # Mutate the worktree file WITHOUT staging/committing — worktree now
    # differs from HEAD (and from the private index, which is read-tree'd
    # fresh from HEAD).
    mutated = "title: orig\nstatus: closed\nEXTRA: mutated-out-from-under-the-reap\n"
    target.write_text(mutated, encoding="utf-8")

    cid = "state/bug-backlog/2026-04-01-modified.yaml"
    reaped, failed = _run(rm_and_commit(fleet_repo.root, [target], "reap: fail-closed test"))

    assert reaped == [], "modified file must not be reaped"
    assert len(failed) == 1
    assert failed[0]["id"] == cid

    assert target.exists(), "modified file must survive — plain git rm refuses before deleting"
    assert target.read_text(encoding="utf-8") == mutated, (
        "file content must be exactly the uncommitted mutation — untouched by the refused rm"
    )


# ---------------------------------------------------------------------------
# AC7 — foreign dirty edit on a swept sibling: fail-closed, not absorbed
# ---------------------------------------------------------------------------


def test_foreign_dirty_sibling_blocks_reap_but_clean_sibling_still_committed(fleet_repo):
    """Batch of two: a foreign-dirty-edited path and a clean path. The dirty
    path's `git rm` refuses (fail-closed — see the module docstring's AC7
    note on why rm_and_commit was never actually FORWARD-B-exploitable), its
    edit survives untouched in the working tree, and it is NOT deleted or
    committed. The clean sibling is still reaped and committed, and that
    commit (sourced purely from the private index, no trailing pathspec)
    carries no trace of the dirty file at all.
    """
    dirty_target = fleet_repo.root / "state" / "bug-backlog" / "2026-04-01-ac7-dirty.yaml"
    dirty_target.parent.mkdir(parents=True, exist_ok=True)
    original = "title: dirty\nstatus: closed\n"
    dirty_target.write_text(original, encoding="utf-8")
    fleet_repo._git("add", str(dirty_target))
    fleet_repo._git("commit", "-m", "add ac7 dirty target")

    clean_target = fleet_repo.root / "state" / "bug-backlog" / "2026-04-01-ac7-clean.yaml"
    clean_target.write_text("title: clean\nstatus: closed\n", encoding="utf-8")
    fleet_repo._git("add", str(clean_target))
    fleet_repo._git("commit", "-m", "add ac7 clean target")

    # Foreign dirty edit on dirty_target — uncommitted, unrelated to the reap.
    foreign_edit = original + "EXTRA: foreign dirty edit unrelated to the reap\n"
    dirty_target.write_text(foreign_edit, encoding="utf-8")

    cid_dirty = "state/bug-backlog/2026-04-01-ac7-dirty.yaml"
    cid_clean = "state/bug-backlog/2026-04-01-ac7-clean.yaml"

    reaped, failed = _run(rm_and_commit(
        fleet_repo.root, [dirty_target, clean_target], "reap: AC7 foreign-dirty-sibling test",
    ))

    assert reaped == [{"id": cid_clean, "reaped": True}], (
        f"only the clean sibling must be reaped; got reaped={reaped}"
    )
    assert len(failed) == 1 and failed[0]["id"] == cid_dirty, (
        f"the dirty sibling must land in failed[], not be silently absorbed; got failed={failed}"
    )

    # Dirty file must survive on disk, byte-identical to the foreign edit —
    # never deleted, never touched by the reap.
    assert dirty_target.exists(), "dirty sibling must not be deleted"
    assert dirty_target.read_text(encoding="utf-8") == foreign_edit, (
        "dirty sibling's content must be exactly the foreign edit — untouched"
    )

    # Clean sibling must be gone.
    assert not clean_target.exists(), "clean sibling must be reaped"

    # The commit that landed must carry no trace of the dirty sibling.
    log_names = subprocess.run(
        ["git", "log", "-1", "--no-renames", "--name-only", "--format="],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    ).stdout.decode().strip().splitlines()
    assert cid_clean in log_names
    assert cid_dirty not in log_names, (
        f"the dirty sibling must not appear in the reap commit; got log_names={log_names!r}"
    )

    # The dirty edit must still show up as a real, unstaged diff afterward —
    # proof it was never staged into (and dropped from) the private index.
    status = fleet_repo._git_unchecked("status", "--porcelain").stdout.decode()
    assert "2026-04-01-ac7-dirty.yaml" in status, (
        f"the foreign edit must remain visibly dirty in git status; got status={status!r}"
    )


def test_pins_git_argv_remove_only_no_pathspec_commit_never_force(fleet_repo, monkeypatch):
    """AC7 mechanism pin: observe rm_and_commit's actual git subprocess argv
    (not assumed from the docstring) for a batch containing one dirty path
    and one clean sibling.

    Pins three claims from rm_and_commit's NEGATIVE-SPEC simultaneously:
      1. Every `git rm` call is the plain `git rm -- <path>` form — `-f` is
         never passed, so the dirty path's rm genuinely fails closed rather
         than being force-pushed through.
      2. The single `git commit` call carries no trailing worktree pathspec
         — it commits from the private index alone, performing no worktree
         read at commit time (the mechanism that makes rm_and_commit safe
         against FORWARD-B by construction, not merely by `git rm`'s refusal).
      3. Every `git update-index` main-index resync call uses `--remove` and
         never `--add` — the reaped file is gone; there is nothing to re-add.

    Asserted against a genuinely dirty path (not a clean-only batch) per the
    plan's "assert it against a dirty path — do not assume it" instruction;
    the dirty path also proves (1) is load-bearing, not vacuously true,
    because a plain `git rm` on a dirty file is exactly the call that would
    need `-f` to succeed, and this test asserts it never gets one.
    """
    dirty_target = fleet_repo.root / "state" / "bug-backlog" / "2026-04-01-argvpin-dirty.yaml"
    dirty_target.parent.mkdir(parents=True, exist_ok=True)
    original = "title: dirty\nstatus: closed\n"
    dirty_target.write_text(original, encoding="utf-8")
    fleet_repo._git("add", str(dirty_target))
    fleet_repo._git("commit", "-m", "add argvpin dirty target")

    clean_target = fleet_repo.root / "state" / "bug-backlog" / "2026-04-01-argvpin-clean.yaml"
    clean_target.write_text("title: clean\nstatus: closed\n", encoding="utf-8")
    fleet_repo._git("add", str(clean_target))
    fleet_repo._git("commit", "-m", "add argvpin clean target")

    # Foreign dirty edit on dirty_target — uncommitted, unrelated to the reap.
    dirty_target.write_text(
        original + "EXTRA: foreign dirty edit unrelated to the reap\n", encoding="utf-8",
    )

    cid_dirty = "state/bug-backlog/2026-04-01-argvpin-dirty.yaml"
    cid_clean = "state/bug-backlog/2026-04-01-argvpin-clean.yaml"
    subject = "reap: AC7 argv pin"

    real_create = asyncio.create_subprocess_exec
    recorded_argv: list = []

    async def _recording_side_effect(*args, **kwargs):
        if args and args[0] == "git":
            recorded_argv.append(list(args))
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _recording_side_effect)

    reaped, failed = _run(rm_and_commit(fleet_repo.root, [dirty_target, clean_target], subject))

    # Behavioural precondition — the argv pins below are meaningless unless
    # this genuinely reproduced fail-closed-on-dirty + clean-sibling-reaped.
    assert reaped == [{"id": cid_clean, "reaped": True}]
    assert len(failed) == 1 and failed[0]["id"] == cid_dirty

    rm_calls = [c for c in recorded_argv if len(c) >= 2 and c[1] == "rm"]
    assert rm_calls, f"expected at least one git rm invocation; got argv={recorded_argv!r}"
    for c in rm_calls:
        assert "-f" not in c, f"rm_and_commit must never pass -f to git rm; got {c!r}"
        assert c[:3] == ["git", "rm", "--"], f"expected plain 'git rm --' form; got {c!r}"

    commit_calls = [c for c in recorded_argv if "commit" in c]
    assert len(commit_calls) == 1, f"expected exactly one git commit call; got {commit_calls!r}"
    commit_argv = commit_calls[0]
    assert commit_argv[-2:] == ["-m", subject], (
        f"expected commit argv to end with '-m {subject!r}' and nothing after; got {commit_argv!r}"
    )
    assert "--" not in commit_argv, (
        f"git commit call must carry no trailing pathspec — it must be index-sourced, "
        f"performing no worktree read at commit time; got {commit_argv!r}"
    )

    update_index_calls = [c for c in recorded_argv if len(c) >= 2 and c[1] == "update-index"]
    assert update_index_calls, (
        f"expected at least one git update-index resync call for the reaped path; "
        f"got argv={recorded_argv!r}"
    )
    for c in update_index_calls:
        assert "--remove" in c, f"resync call must use --remove; got {c!r}"
        assert "--add" not in c, (
            f"rm_and_commit's resync must never use --add — the reaped file is deleted, "
            f"there is nothing to re-add; got {c!r}"
        )


# ---------------------------------------------------------------------------
# commit-failure reversal (restore-from-HEAD)
# ---------------------------------------------------------------------------


def test_commit_failure_restores_reaped_files_from_head(fleet_repo):
    """When the commit fails, every git-rm'd file is restored from HEAD
    (never a rename-back — there is no dst for a delete) and lands in failed[]."""
    target = fleet_repo.root / "state" / "bug-backlog" / "2026-04-01-commitfail.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    content = "title: cf\nstatus: closed\n"
    target.write_text(content, encoding="utf-8")
    fleet_repo._git("add", str(target))
    fleet_repo._git("commit", "-m", "add commit-failure target")

    head_before = _head_sha(fleet_repo)
    _write_hook_that_fails(fleet_repo)

    cid = "state/bug-backlog/2026-04-01-commitfail.yaml"
    reaped, failed = _run(rm_and_commit(fleet_repo.root, [target], "reap: commit-failure test"))

    assert reaped == []
    assert len(failed) == 1
    assert failed[0]["id"] == cid
    assert "commit-failed" in failed[0]["reason"]

    assert target.exists(), "file must be restored from HEAD after commit failure"
    assert target.read_text(encoding="utf-8") == content

    head_after = _head_sha(fleet_repo)
    assert head_after == head_before, "no commit must have landed when commit fails"


# ---------------------------------------------------------------------------
# idempotent no-op
# ---------------------------------------------------------------------------


def test_idempotent_noop_empty_paths(fleet_repo):
    """rm_and_commit(worktree, [], subject) -> ([], []), no commit created."""
    head_before = _head_sha(fleet_repo)

    reaped, failed = _run(rm_and_commit(fleet_repo.root, [], "reap: noop"))

    assert reaped == []
    assert failed == []

    head_after = _head_sha(fleet_repo)
    assert head_after == head_before, "empty paths must not create a commit"
    assert fleet_repo.git_status_clean()
