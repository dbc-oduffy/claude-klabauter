"""
coordinator_core.ops.fleet.tests.test_archive_and_commit_forward_b_hazard

C4 of docs/plans/2026-08-13-archive-family-coverage-restoration.md.

DISPOSITION: the two culled tests in this module (`test_foreign_dirty_edit_
on_dst_excluded_from_commit_and_survives_in_worktree`, parametrized over
memo/handoff, 1 `def test_` function culled) are DROPPED-AS-OBSOLETE, not
ported. Reason, verified against HEAD 2026-09-19:

The FORWARD-B mechanism this module regression-tested no longer exists.
`archive_and_commit` (coordinator_core/ops/fleet/_common.py) used to land its
single end-of-batch commit via a private HEAD-seeded GIT_INDEX_FILE dance
ending in a plain `git commit`, and the FORWARD-B fix (the fact under test
here) was about that `git commit` call dropping a trailing `-- <pathspec>`
that would otherwise silently re-read WORKTREE content for the named paths
at commit time. That whole mechanism was replaced (2026-08-26, "the archival
seam stops asking git at all") by `_commit_via_head_spine`: the commit's
tree is assembled directly, in-process, from the batch's own `{path: (mode,
sha) | _ABSENT}` delta and handed to a locked compare-and-swap ref update —
there is no `git commit` subprocess call left anywhere in `archive_and_
commit`, pathspec'd or otherwise, and therefore no window in which a
foreign edit landing on a just-`os.replace`'d destination could be absorbed
by a *worktree-re-reading* commit step. The race this module exercised
cannot recur through the mechanism that used to produce it; a REPLACEMENT
race (if any) at the `_commit_via_head_spine` seam is a new investigation,
not a port of this one, and is out of this chunk's scope.

Negative-spec: do NOT resurrect a `fleet_repo`-style ambient fixture or the
old `_inject_foreign_dirty_edit_after_mv` `git mv`-interception helper to
"keep the coverage" — there is no `git mv` call left to intercept either
(os.replace, in-process, per Move's own docstring in _common.py).

What replaces it, git-free (0 real-git test functions, AC5-neutral): a
structural regression net pinning the invariant this module's whole premise
depended on — that `archive_and_commit`'s commit step never re-reads the
worktree for its own paths. If a future edit reintroduces a `git commit`
(or any subprocess) call inside `archive_and_commit` that references a
pathspec, this guard fails loud rather than silently reopening the
FORWARD-B class.

Spec backlinks:
  - Original coverage: docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md
  - Retirement: docs/plans/2026-08-26-the-archival-seam-stops-asking-git-at-all.md
  - This chunk: docs/plans/2026-08-13-archive-family-coverage-restoration.md, C4
"""

from __future__ import annotations

import inspect

from coordinator_core.ops.fleet import _common


def test_archive_and_commit_never_shells_a_pathspecd_git_commit():
    """`archive_and_commit`'s own source must not contain a `git`/`commit`
    subprocess invocation carrying a trailing pathspec — the FORWARD-B
    mechanism this module used to regression-test. Lands the commit via
    `_commit_via_head_spine` (in-process tree assembly + CAS ref update)
    instead; asserting the source calls that helper, and does not itself
    spawn `git commit`, is the cheapest structural proxy for "the worktree
    re-read window this module pinned does not exist"."""
    src = inspect.getsource(_common.archive_and_commit)

    assert "_commit_via_head_spine(" in src, (
        "archive_and_commit must land its batch commit via the in-process "
        "spine helper, not a shelled `git commit` — if this assertion "
        "fails, the FORWARD-B hazard class this module used to guard "
        "against may have been reintroduced and this chunk's dropped-as-"
        "obsolete disposition needs revisiting, not silently accepting."
    )
    assert '"commit"' not in src and "'commit'" not in src, (
        "archive_and_commit must not itself shell a `git commit` (pathspec'd "
        "or otherwise) — that subprocess call, and the worktree-re-read "
        "window it opened, is exactly the mechanism FORWARD-B fixed and "
        "_commit_via_head_spine retired outright."
    )
