"""Chunk C4 (docs/plans/2026-08-18-session-fact-facade-and-failure-posture.md):
`worktree_is_dirty` converted onto DR-319's degraded-with-evidence posture
(docs/decisions/DR-319-session-fact-facade-shape-and-failure-posture.md) —
a failed `git status` probe must never read as a clean tree.

Reference failure-posture shape (read-only): `coordinator_core/baton_assemble/
__init__.py :: _compute_dirty_tree_attribution`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.consolidate_assemble import brief, worktree_is_dirty


def _run_git(returncode: int, stdout: str, stderr: str = ""):
    def _fake(argv: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    return _fake


_STUB_WORKTREE = "stub-worktree"  # opaque token, never touches disk -- run_git is faked


def test_clean_tree_is_computed_not_dirty():
    result = worktree_is_dirty(_run_git(0, ""), _STUB_WORKTREE)
    assert result == {"degraded": False, "value": False}


def test_dirty_tree_is_computed_dirty():
    result = worktree_is_dirty(_run_git(0, " M some/file.py\n"), _STUB_WORKTREE)
    assert result == {"degraded": False, "value": True}


def test_failed_probe_is_degraded_not_clean():
    """The defect this chunk fixes: `bool("".strip())` on a failed call's
    empty stdout is `False` — indistinguishable from a genuinely clean tree.
    A failed `git status` must produce a structurally different shape."""
    result = worktree_is_dirty(_run_git(128, "", "fatal: not a git repository"), _STUB_WORKTREE)
    assert result["degraded"] is True
    assert "value" not in result
    assert "fatal: not a git repository" in result["evidence"]
    # The regression this test exists to catch: a degraded read must never
    # collapse to the same value a clean computed read would produce.
    assert result != {"degraded": False, "value": False}


_MY_EMAIL = "me@example.com"
_STALE_WORKTREE = "stub-worktree-other"

_WORKTREE_PORCELAIN = (
    "worktree stub-repo-root\n"
    "HEAD abc1230000000000000000000000000000000000\n"
    "branch refs/heads/work\n"
    "\n"
    "worktree stub-worktree-other\n"
    "HEAD def4560000000000000000000000000000000000\n"
    "branch refs/heads/stale-branch\n"
)


def _fake_run_git_degraded_dirty_probe(argv: list[str], cwd: Path):
    """Routes every git call `brief()` makes for a repo with exactly one
    stale, author-owned, main-reachable worktree whose dirty-tree probe
    fails (`git status --porcelain` exits non-zero). Every other call
    returns a fixed, uninteresting answer so the only variable under test
    is the degraded probe reaching the call site."""
    if argv[:2] == ["rev-parse", "--abbrev-ref"]:
        stdout = "work\n"
    elif argv[:2] == ["rev-parse", "--verify"]:
        stdout = "main\n" if argv[-1] == "main" else ""
    elif argv[0] == "branch":
        stdout = "  work\n  main\n"
    elif argv[0] == "worktree":
        stdout = _WORKTREE_PORCELAIN
    elif argv[0] == "log":
        stdout = f"{_MY_EMAIL}\n"
    elif argv[0] == "merge-base":
        stdout = ""  # returncode 0 below -> reachable
    elif argv[:2] == ["--no-optional-locks", "status"]:
        return subprocess.CompletedProcess(
            argv, 128, stdout="", stderr="fatal: not a git repository"
        )
    else:
        stdout = ""
    return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def test_degraded_dirty_probe_call_site_routes_through_judgment_point():
    """Finding 1 of the slice-B review, made an executable regression: a
    degraded dirty-tree probe reaching `brief()`'s call site must not take
    the unconditional-removal path a genuinely-clean tree would unlock. It
    must route through the same judgment-point-gated path a dirty tree
    does, and the served `worktrees_report` entry must carry
    `dirty_probe_degraded` so a caller can tell the two cases apart."""
    decision_object = brief(
        repo_root=Path("stub-repo-root"),
        my_email=_MY_EMAIL,
        run_git=_fake_run_git_degraded_dirty_probe,
    )

    worktrees_report = decision_object["gates"]["worktrees"]
    stale_entry = next(w for w in worktrees_report if w["path"] == _STALE_WORKTREE)
    assert stale_entry["dirty_probe_degraded"] is True
    assert stale_entry["dirty"] is True

    remove_directives = [
        d
        for d in decision_object["directives"]
        if d["cli"] == "worktree-remove" and d["args"] == [_STALE_WORKTREE]
    ]
    assert len(remove_directives) == 1
    remove_directive = remove_directives[0]
    # The regression this test exists to catch: a degraded probe must never
    # produce a `depends_on: None` (unconditional-removal) directive — that
    # is the fail-open collapse the original defect authorized.
    assert remove_directive["depends_on"] is not None

    jp_id = remove_directive["depends_on"]
    matching_jps = [jp for jp in decision_object["judgment_points"] if jp["id"] == jp_id]
    assert len(matching_jps) == 1
    assert "cannot confirm clean" in matching_jps[0]["question"]
    assert matching_jps[0]["evidence"]["probe_degraded"] is True
