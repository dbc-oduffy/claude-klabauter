"""
coordinator_core.ops.fleet.tests.test_empty_private_index_refusal

Regression pin for the 2026-08-18 branch-collapse incident: commit
`fbfbd061d` ("fleet: archive 1 actioned memo(s)", via
fleet.archive_actioned_memos) committed a tree of
`4b825dc642cb6eb9a060e54bf8d69288fbee4904` -- git's canonical EMPTY TREE --
deleting all 26,264 tracked files on an already-pushed shared branch. Nine
further commits then accreted on top of the emptied HEAD before it was
caught.

Mechanism (verified on git 2.55.0.windows.4, not inferred):

  * `git write-tree` against a MISSING `GIT_INDEX_FILE` returns the empty
    tree with **rc=0 and empty stderr** -- silent.
  * A ZERO-BYTE index instead fails loud (rc=128, "index file smaller than
    expected").

So an index that vanishes AFTER a successful `read-tree HEAD` seed is
invisible to every `.ok`/returncode check upstream of the commit. Both
commit seams in `_common.py` commit from the private index with NO trailing
pathspec -- correct, and required, to close the FORWARD-B worktree-
absorption hazard -- which is precisely why a lost index there deletes the
repo instead of committing nothing.

The refusal under test is deliberately trigger-independent: what removed the
index is still open, and the guard holds regardless.

Real-git spawn is load-bearing here for the same reason the sibling
`test_archive_and_commit_disk_head_drift.py` gives: a mocked git has no
index to lose. Own small scoped module, exactly one throwaway repo, per the
test-cull ruling (state/audits/2026-08-07-spawn-heavy-test-excision-
ledger.md) that real-git fixtures must not go ambient again.

Spec backlink: coordinator_core/ops/fleet/_common.py ::
_empty_private_index_breach
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet._common import (
    EMPTY_TREE_SHA,
    Move,
    _empty_private_index_breach,
    archive_and_commit,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git spawn, mirrors the
    # governed real_git.py fixture's own unguarded pattern.
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _run(coro):
    return asyncio.run(coro)


def _seed_repo(root: Path) -> Path:
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)

    inbox = root / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    src = inbox / "2026-08-18-peer-memo.md"
    src.write_text("---\nstatus: actioned\n---\n\nBody.\n", encoding="utf-8")

    # Bystander files: these are what an empty-tree commit would delete. The
    # incident's severity was entirely in this population, not in the one
    # path the op meant to touch.
    for i in range(5):
        (root / f"bystander-{i}.txt").write_text(f"content {i}\n", encoding="utf-8")

    _git(["add", "--", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return src


def test_missing_index_reports_empty_tree_and_names_the_sha(tmp_path: Path):
    """A MISSING index file is the silent case -- write-tree returns the empty
    tree with rc=0 -- so the refusal must fire on the sha equality, and its
    message must name `4b825dc…` rather than leaving a future reader to
    rediscover what that constant means."""
    root = tmp_path / "repo"
    _seed_repo(root)

    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(tmp_path / "index-that-does-not-exist")

    reason, tree_sha = _run(_empty_private_index_breach(root, env, "archive_and_commit"))

    assert reason is not None, "a missing index must never be reported as safe to commit"
    assert "empty-private-index" in reason
    assert EMPTY_TREE_SHA in reason, "the refusal must name the empty tree explicitly"


def test_zero_byte_index_reports_unreadable_not_empty(tmp_path: Path):
    """The loud case must stay distinguishable from the silent one: a
    truncated index is a different fault from a vanished one, and collapsing
    them would misdirect whoever reads the failure."""
    root = tmp_path / "repo"
    _seed_repo(root)

    zero_byte = tmp_path / "index-zero"
    zero_byte.write_bytes(b"")
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(zero_byte)

    reason, tree_sha = _run(_empty_private_index_breach(root, env, "archive_and_commit"))

    assert reason is not None
    assert "private-index-unreadable" in reason
    assert "empty-private-index" not in reason


def test_seeded_index_is_permitted(tmp_path: Path):
    """The guard must not fire on the ordinary path -- a HEAD-seeded index
    commits normally. Without this, a refusal that always fired would 'pass'
    the two tests above while breaking every fleet archival."""
    root = tmp_path / "repo"
    _seed_repo(root)

    private_index = tmp_path / "index-seeded"
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(private_index)
    _git(["read-tree", "HEAD"], root)  # sanity: HEAD is readable
    subprocess.run(
        ["git", "read-tree", "HEAD"],
        cwd=str(root), env=env, capture_output=True, text=True, check=True,
    )

    reason, tree_sha = _run(_empty_private_index_breach(root, env, "archive_and_commit"))
    assert reason is None
    # The guard hands its tree sha back so the caller commits THAT tree instead
    # of re-spawning an identical `git write-tree` (2026-08-25 de-duplication).
    assert tree_sha and tree_sha != EMPTY_TREE_SHA


def test_archive_and_commit_refuses_rather_than_emptying_the_repo(tmp_path: Path):
    """End-to-end pin on the incident's CLASS, RE-SITED (2026-08-26, C7).

    `archive_and_commit` no longer opens a `GIT_INDEX_FILE` at all
    (`dccf2fc01`, C2) -- there is no private index left to go missing, so the
    exact `fbfbd061d` failure mode (delete the index between seed and
    commit) cannot recur on this path, and simulating it here would be a
    false positive: the interception would never fire and the test would
    pass vacuously. See `_assembled_commit_is_noop`'s own docstring for the
    re-siting this test now pins: the guarantee that survives is "never land
    a commit whose assembled tree is byte-identical to HEAD's tree", checked
    the same way -- loud, before the commit, before any disk state is
    committed.

    Fixture: `dst` is ALREADY tracked in HEAD with the exact bytes and mode
    the move would (redundantly) write there again, and `src` is untracked
    in HEAD (so its removal from the tree is not a real deletion either) --
    every entry `archive_and_commit` would assemble already matches what
    `read_tree_spine` reports for HEAD, so `_assembled_commit_is_noop`
    refuses before `_commit_via_head_spine` is ever called.
    """
    root = tmp_path / "repo"
    _seed_repo(root)
    identical_bytes = "already archived, byte-identical\n"

    dst = root / "cross-repo" / "archive" / "already-archived.md"
    dst.parent.mkdir(parents=True)
    dst.write_text(identical_bytes, encoding="utf-8")
    _git(["add", "--", str(dst)], root)
    _git(["commit", "-q", "-m", "seed: dst already archived"], root)

    head_before = _git(["rev-parse", "HEAD"], root).stdout.strip()
    tracked_before = _git(["ls-tree", "-r", "--name-only", "HEAD"], root).stdout.split()

    src = root / "cross-repo" / "inbox" / "already-archived.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(identical_bytes, encoding="utf-8")  # untracked in HEAD

    # force=True: a byte-identical duplicate delivery, per `Move.force`'s own
    # docstring -- os.replace always clobbers, and dst already existing is
    # exactly the shape this fixture needs to reach the noop-commit guard
    # rather than the earlier dst-exists refusal.
    move = Move(
        src=src, dst=dst, candidate_id="cross-repo/inbox/already-archived.md",
        force=True,
    )

    acted, failed = _run(
        archive_and_commit(
            worktree_root=root,
            moves=[move],
            subject="fleet: archive 1 actioned memo(s)",
        )
    )

    assert acted == [], "nothing may be reported as archived when the commit was refused"
    assert len(failed) == 1
    assert "empty-spine-commit" in failed[0]["reason"]
    assert "computed tree equals HEAD's tree" in failed[0]["reason"]

    # The load-bearing assertions: HEAD did not move, and no tracked file was
    # touched.
    assert _git(["rev-parse", "HEAD"], root).stdout.strip() == head_before
    tracked_after = _git(["ls-tree", "-r", "--name-only", "HEAD"], root).stdout.split()
    assert tracked_after == tracked_before

    # The rename was reversed on disk — a refused commit leaves no half-move.
    # (os.replace moved src's bytes onto dst; the commit-failure reversal
    # renames dst back to src, since src no longer exists post-replace.)
    assert src.exists()
    assert not dst.exists()
