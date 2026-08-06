"""
coordinator_core.tests.test_git_ancestry — mutation-killing coverage for
git_ancestry.is_covered's load-bearing polarity (AC11).

The predicate under test is:
    is_covered(commit, start, end) == is-ancestor(commit, end) AND NOT is-ancestor(commit, start)

Coverage:
  (a) Parity fixture mirroring plan-delivery-audit's 2026-05-27 example-game-repo worked
      example shape (a start/end range with several delivery commits, all
      covered) — this repo cannot reach the actual example-game-workbench-repo SHAs
      cited in that worked example, so the fixture reproduces the same
      predicate structure (a linear chain, an exclusive start boundary, an
      inclusive end boundary, multiple in-range commits) on a synthetic repo.
  (b) Explicit negative-clause regression: a commit predating the window start
      MUST be excluded.
  (c) Boundary case at exactly start (A) — excluded (start is exclusive).
  (d) Boundary case at exactly end (B) — included (end is inclusive).
  (e) Mutation-kill check: inverting clause (2)'s polarity (dropping the `not`)
      must turn cases (b)-(d) red — asserted directly by comparing against the
      known-mutant formula.

Spec backlink: docs/plans/2026-07-24-computed-skills-b5-planning-cluster.md § C1
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

from coordinator_core.git_ancestry import is_covered, is_ancestor, _is_ancestor


# ---------------------------------------------------------------------------
# Git repo helper (mirrors test_coverage_reviewed_set.py convention)
# ---------------------------------------------------------------------------

def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command in cwd; raise on non-zero exit."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        encoding="utf-8",
        check=True,
    )


def _make_commit(repo: Path, message: str) -> str:
    """Make an empty commit in repo and return its full SHA."""
    _git(["commit", "--allow-empty", "-m", message], repo)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    """Initialise a fresh git repo with required identity config."""
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


@pytest.fixture
def chain(tmp_path: Path) -> dict:
    """Linear chain: before_a -> a (start) -> mid1 -> mid2 -> b (end)."""
    _init_repo(tmp_path)
    shas = {}
    shas["before_a"] = _make_commit(tmp_path, "before window start")
    shas["a"] = _make_commit(tmp_path, "window start (exclusive)")
    shas["mid1"] = _make_commit(tmp_path, "delivery commit 1")
    shas["mid2"] = _make_commit(tmp_path, "delivery commit 2")
    shas["b"] = _make_commit(tmp_path, "window end (inclusive)")
    shas["repo"] = tmp_path
    return shas


def test_parity_worked_example_shape(chain: dict) -> None:
    """Mirrors plan-delivery-audit's 2026-05-27 example-game-repo worked example: multiple
    delivery commits within a start..end range are all reported covered."""
    repo = chain["repo"]
    for key in ("mid1", "mid2", "b"):
        assert is_covered(chain[key], chain["a"], chain["b"], cwd=str(repo)) is True


def test_negative_clause_excludes_pre_window_commit(chain: dict) -> None:
    """A commit predating the window start MUST be excluded — the explicit
    negative-clause regression AC11 requires."""
    repo = chain["repo"]
    assert is_covered(chain["before_a"], chain["a"], chain["b"], cwd=str(repo)) is False


def test_boundary_exactly_at_start_is_excluded(chain: dict) -> None:
    """start (A) itself is excluded — the range is exclusive of its start."""
    repo = chain["repo"]
    assert is_covered(chain["a"], chain["a"], chain["b"], cwd=str(repo)) is False


def test_boundary_exactly_at_end_is_included(chain: dict) -> None:
    """end (B) itself is included — the range is inclusive of its end."""
    repo = chain["repo"]
    assert is_covered(chain["b"], chain["a"], chain["b"], cwd=str(repo)) is True


def test_mutation_kill_inverted_start_clause(chain: dict) -> None:
    """A mutation dropping the `not` on the start-ancestor clause (is-ancestor-of-end
    AND is-ancestor-of-start, instead of AND NOT) must diverge from is_covered on
    every case above — pinning that the current polarity is load-bearing."""
    repo = chain["repo"]

    def mutant(commit: str, start: str, end: str) -> bool:
        return _is_ancestor(commit, end, cwd=str(repo)) and _is_ancestor(commit, start, cwd=str(repo))

    for key in ("before_a", "a", "mid1", "mid2", "b"):
        correct = is_covered(chain[key], chain["a"], chain["b"], cwd=str(repo))
        mutated = mutant(chain[key], chain["a"], chain["b"])
        assert correct != mutated, f"mutant did not diverge for {key!r}"


def test_legacy_wrapper_git_not_on_path_fails_closed_not_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review: code-reviewer — Finding 3. Pins `_is_ancestor`'s new fail-closed
    contract on the git-missing exception path (mirrors
    `test_sibling_fact.py::test_commit_ancestor_git_not_on_path_is_indeterminate`,
    which covers the public `is_ancestor` but not this legacy bare-bool wrapper).
    Before the `is_ancestor` promotion, `_is_ancestor` called `subprocess.run`
    directly with no try/except, so a missing `git` binary raised an uncaught
    `FileNotFoundError` out of this function. It now delegates to `is_ancestor`,
    which catches `OSError`/`TimeoutExpired` and returns `(False, None)` — so
    `_is_ancestor` must return `False` here, not raise."""

    def _raise_missing_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _raise_missing_git)
    assert _is_ancestor("abc123", "def456") is False
    read_ok, observed = is_ancestor("abc123", "def456")
    assert read_ok is False
    assert observed is None
