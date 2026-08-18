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

from coordinator_core.consolidate_assemble import worktree_is_dirty


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
