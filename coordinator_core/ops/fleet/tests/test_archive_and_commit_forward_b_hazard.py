"""
coordinator_core.ops.fleet.tests.test_archive_and_commit_forward_b_hazard

Regression net for the FORWARD-B fix (C4): dropping the trailing `-- <paths>`
pathspec from archive_and_commit's `git commit` call so the commit is sourced
purely from the private GIT_INDEX_FILE, never from worktree content on the
named paths.

FORWARD-B mechanism: `git commit -- <pathspec>` silently re-reads WORKTREE
content for the named paths at commit time, overriding whatever was staged in
the index for them. If a foreign process edits a just-archived destination
file in the window between archive_and_commit's git-mv step and its single
end-of-batch commit, the OLD (pathspec'd) commit call absorbed that foreign
edit into the archival commit and misattributed it to the archival subject
line — the mechanism that laundered 34 hand-edited memo frontmatter changes
into fleet-archival commits on 2026-07-26. The fix (private-index-only commit,
no trailing pathspec) must commit exactly what git-mv staged and leave the
foreign edit sitting dirty in the working tree afterward.

Coverage:
  - AC7/AC8 (parametrized memo + handoff family): a destination path foreign-
    edited between git-mv and commit is committed WITHOUT that edit; the
    foreign edit survives, dirty, in the working tree after the op returns.
    Both families exercise the SAME archive_and_commit helper (AC8 rides on
    AC7's fix — no family-specific code path exists to diverge).

Spec backlinks:
  - Plan (C4): docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md (D3/D4,
    FORWARD-B amendment, C4a)
  - Fixed site: coordinator_core/ops/fleet/_common.py archive_and_commit
  - Pattern mirrors: test_fleet_common.py test_archive_and_commit_private_index_isolation
    (proves a STAGED main-index sentinel is not absorbed); this file proves the
    companion UNSTAGED/dirty-worktree hazard on the op's OWN destination path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.fleet._common import Move, archive_and_commit


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


class _FakeCompletedProc:
    """Stand-in for asyncio.subprocess.Process exposing only what archive_and_commit
    reads off a completed process: .returncode and an awaitable .communicate()."""

    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


def _inject_foreign_dirty_edit_after_mv(target_dst: Path, dirty_content: bytes):
    """Build an asyncio.create_subprocess_exec replacement that races a foreign
    dirty edit onto target_dst immediately after archive_and_commit's own `git mv`
    for that path completes — simulating the window between git-mv and the
    end-of-batch commit that FORWARD-B exploited.

    Every OTHER subprocess call (git read-tree, the final git commit) passes
    through unmodified to the REAL asyncio.create_subprocess_exec — only the
    one `git mv ... target_dst` invocation is intercepted.
    """
    real_create = asyncio.create_subprocess_exec

    async def _side_effect(*args, **kwargs):
        is_mv_to_target = (
            len(args) >= 2
            and args[0] == "git"
            and args[1] == "mv"
            and str(target_dst) in args
        )
        if not is_mv_to_target:
            return await real_create(*args, **kwargs)

        proc = await real_create(*args, **kwargs)
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            target_dst.write_bytes(dirty_content)
        return _FakeCompletedProc(proc.returncode, stdout, stderr)

    return _side_effect


# ---------------------------------------------------------------------------
# AC7/AC8 — foreign dirty edit on a just-moved dst is excluded from the
# commit and survives dirty in the working tree, for both fleet families
# archive_and_commit serves.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "family,src_rel,dst_rel",
    [
        ("memo", "cross-repo/inbox/2026-07-26-forward-b.md", "cross-repo/archive/2026-07-26-forward-b.md"),
        ("handoff", "state/handoffs/2026-07-26-forward-b.md", "archive/handoffs/2026-07/2026-07-26-forward-b.md"),
    ],
)
def test_foreign_dirty_edit_on_dst_excluded_from_commit_and_survives_in_worktree(
    fleet_repo, family, src_rel, dst_rel,
):
    """A foreign dirty edit landing on the archival destination between git-mv
    and archive_and_commit's single end-of-batch commit is NOT committed — the
    commit carries exactly what git-mv staged — and the foreign edit remains,
    unstaged and dirty, in the working tree after the op returns.
    """
    clean_content = f"---\ntitle: {family} under archival\nstatus: actioned\n---\n\nClean body.\n"
    src = fleet_repo.root / src_rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(clean_content, encoding="utf-8")
    fleet_repo._git("add", str(src))
    fleet_repo._git("commit", "-m", f"add {family} candidate")

    dst = fleet_repo.root / dst_rel
    candidate_id = fleet_repo.repo_rel(src)
    dst_rel_posix = fleet_repo.repo_rel(dst)

    dirty_content = f"FOREIGN DIRTY EDIT unrelated to the {family} archival\n".encode("utf-8")

    moves = [Move(src=src, dst=dst, candidate_id=candidate_id)]
    with patch(
        "asyncio.create_subprocess_exec",
        new=_inject_foreign_dirty_edit_after_mv(dst, dirty_content),
    ):
        acted, failed = _run(archive_and_commit(
            fleet_repo.root, moves, f"archive({family}): FORWARD-B regression test",
        ))

    assert acted == [{"id": candidate_id, "archived": True}], (
        f"the move must still be classified as acted despite the race; got acted={acted} failed={failed}"
    )
    assert failed == []

    # The COMMITTED content at dst must be the clean, git-mv'd content — NOT
    # the foreign dirty edit. Read via `git show HEAD:<path>` (committed blob),
    # not the worktree file, since the worktree now carries the foreign edit.
    committed = fleet_repo._git("show", f"HEAD:{dst_rel_posix}").stdout.decode("utf-8")
    assert committed == clean_content, (
        "the archival commit must contain the git-mv'd content, not the foreign "
        f"dirty edit; got committed content: {committed!r}"
    )

    # The foreign edit must SURVIVE, dirty, in the actual working-tree file —
    # archive_and_commit must not have clobbered or reverted it.
    assert dst.read_bytes() == dirty_content, (
        "the foreign dirty edit must remain in the working tree after the op returns"
    )

    # And it must show up as a real, unstaged dirty diff against the commit
    # that just landed — proof it was never absorbed into that commit.
    status = fleet_repo._git_unchecked("status", "--porcelain").stdout.decode("utf-8")
    assert dst_rel_posix in status, (
        f"the foreign edit must appear as dirty in git status; got status={status!r}"
    )
