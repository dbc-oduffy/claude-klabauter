"""
coordinator_core.tests.test_coverage_dag_chain_set_cross_branch — regression test
for DAG-mode chain_set segment-attribution over-attribution (improvement-queue
2026-07-01-chain-end-coverage-gate-dag-mode-over-at.yaml and
2026-07-03-coverage-gate-dag-mode-misfires-on-prede.yaml).

Root cause (coordinator_core/coverage.py, _derive_dag_chain_set Step 3, prior to
this fix): once a coverable node's authoring Session-Id is resolved, its credited
commit segment was built via

    git log --all --no-merges --format=%H --grep=^Session-Id: <sid>$

`--all` walks EVERY ref in the repository -- every other local/remote branch,
every worktree's branch, every tag -- not just the history reachable from the
checkout that is actually being closed out. A commit carrying the SAME
Session-Id trailer but living on an entirely unrelated branch (never merged,
never an ancestor of the closing handoff's own HEAD) was swept into chain_set
regardless. Because build_reviewed_set's ancestry-based reviewed_set can never
cover a commit that is not even an ancestor of HEAD, this guarantees a false
UNCOVERED verdict over commits that were never part of the workstream being
closed -- exactly the "17 uncovered commits, all non-code, over-attributed"
and "sibling commits ... swept in" symptoms both improvement-queue entries
describe.

Fix: scope the segment query to `HEAD` instead of `--all`. This does not (by
itself) resolve same-branch concurrent-session contamination -- a sibling EM's
commits interleaved on a shared work/* branch remain reachable from HEAD too,
and are excluded (or not) purely by whether the Session-Id trailer actually
differs -- but it closes the strictly-broader "unrelated branch/worktree" hole,
which is unconditionally wrong regardless of session attribution.

This test MUST fail against the pre-fix code (git log --all) and pass after
(git log HEAD).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

from coordinator_core import coverage as cov
from coordinator_core import dag


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


@pytest.fixture(autouse=True)
def clear_frontmatter_cache():
    dag._FRONTMATTER_CACHE.clear()
    yield
    dag._FRONTMATTER_CACHE.clear()


def test_derive_dag_chain_set_excludes_unrelated_branch_commit_same_session_id(
    tmp_path: Path,
) -> None:
    """A commit on an unrelated, unmerged branch that happens to carry the SAME
    Session-Id trailer as the closing session must not enter chain_set: it is
    not an ancestor of HEAD, so it cannot possibly be part of the workstream
    being closed out on this checkout.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    session_id = "44444444-4444-4444-4444-444444444444"

    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    closing = handoffs / "closing.md"
    closing.write_text("---\nsession_id: s1\npredecessor: none\n---\nClosing body.\n")
    _git(["add", "state/handoffs/closing.md"], repo)
    _git(
        [
            "commit", "-m",
            f"add closing handoff\n\nSession-Id: {session_id}",
        ],
        repo,
    )
    on_branch_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    # An entirely unrelated, never-merged branch, with a commit carrying the
    # identical Session-Id trailer -- simulating the same authoring session
    # having done unrelated work elsewhere (a different checkout / worktree /
    # abandoned feature branch) that must never be credited to THIS workstream.
    _git(["checkout", "-b", "unrelated-branch"], repo)
    stray = repo / "stray.txt"
    stray.write_text("unrelated work\n")
    _git(["add", "stray.txt"], repo)
    _git(
        [
            "commit", "-m",
            f"unrelated work on a different branch\n\nSession-Id: {session_id}",
        ],
        repo,
    )
    off_branch_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    _git(["checkout", "main"], repo)

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is False, f"unexpected INDETERMINATE: {result.notes!r}"
    assert on_branch_sha in result.shas, (
        "the closing session's own on-branch (HEAD-ancestor) commit must "
        f"still be credited; shas={result.shas!r}"
    )
    assert off_branch_sha not in result.shas, (
        "a commit on an unrelated, unmerged branch must NOT enter chain_set "
        "merely because it carries the same Session-Id trailer -- it is not "
        f"an ancestor of HEAD and cannot be part of this workstream; shas={result.shas!r}"
    )
