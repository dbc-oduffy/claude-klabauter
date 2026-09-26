"""
coordinator_core.pickup_assemble.tests.test_read_only_invariant — pins the
module docstring's READ-ONLY guarantee (AC2b/AC3) that, until now, was
asserted only in prose.

Spec backlink: state/audits/2026-08-13-pickup-negative-spec-adjudication.md § 7
Baton: state/handoffs/2026-08-13-reconcile-evidence-at-pickup-cadence.md

The existing `test_brief_mutates_nothing_on_disk` (coordinator_core/
test_pickup_assemble.py) only checks `git status --porcelain`, which is a
worktree-only view — it cannot see a write landing under `.git/` itself
(session/claim/history state, say) because untracked writes inside the git
common dir never show up in porcelain output at all. This file closes that
gap: it snapshots BOTH the worktree and the git common dir before and after
a `brief()` call and asserts neither changed. The `apply` path is out of
scope — it writes by design.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.pickup_brief as pa

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


from coordinator_core.pickup_assemble.tests._git_harness import (
    git as _git,
    init_repo as _init_repo,
)


def _seed_handoff(repo: Path, name: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: active\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _snapshot(root: Path) -> set[tuple[str, int, int]]:
    snap: set[tuple[str, int, int]] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.parent.name == "__pycache__" and path.suffix == ".pyc":
            continue
        st = path.stat()
        snap.add((str(path.relative_to(root)), st.st_mtime_ns, st.st_size))
    return snap


class TestBriefReadOnlyAcrossWorktreeAndGitCommonDir:
    def test_brief_writes_nothing_under_worktree_or_git_dir(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        git_dir = repo / ".git"
        tree_before = _snapshot(repo)
        git_before = _snapshot(git_dir)

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        tree_after = _snapshot(repo)
        git_after = _snapshot(git_dir)

        assert result.exit_code == pa.EXIT_OK
        assert tree_before == tree_after
        assert git_before == git_after
