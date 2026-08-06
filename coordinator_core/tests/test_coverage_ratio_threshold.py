"""
coordinator_core.tests.test_coverage_ratio_threshold — watched-to-fail
regression for C10 (docs/plans/2026-08-05-coverage-gate-planning-artifact-
class.md): `run_coverage_gate`'s binary
`verdict = "COVERED" if not uncovered_shas else "UNCOVERED"` test is replaced
by a coverage RATIO over the code partition, WARNing (never blocking) below
DEFAULT_COVERAGE_RATIO_THRESHOLD (~66%).

Watched-to-fail bar (plan-quoted, verbatim): "Every new check has been
watched to fail. Construct the negative case (a range that should warn, a
mixed commit that should classify as CODE) and confirm it fails before the
fix and passes after. Report the actual output. This repo has 36 lessons on
checks that pass while testing nothing; a coverage gate that silently
returns COVERED for everything is the single most dangerous outcome of this
change and the easiest to ship by accident."

Both tests below share a 3-code-commit fixture and differ ONLY in how many
of the 3 carry a trail record — proving the verdict actually moves with the
ratio, not a constant:
  - test_above_threshold_ratio_is_covered: 2/3 covered (~66.7%) >= threshold
    -> VERDICT=COVERED.
  - test_below_threshold_ratio_is_warn: 1/3 covered (~33.3%) < threshold
    -> VERDICT=WARN, carrying the coordinator:review-code remediation offer,
    never a block (exit_code == 0).

Pre-C10 baseline (what this pins as fixed): BOTH cases above would have
resolved to VERDICT=UNCOVERED under the old binary test, because both have
>=1 uncovered commit. A run of these tests against coverage.py at the
pre-C10 revision (`verdict = "COVERED" if not uncovered_shas else
"UNCOVERED"`, no ratio/threshold) fails test_above_threshold_ratio_is_covered
(asserts "COVERED", gets "UNCOVERED") while
test_below_threshold_ratio_is_warn would have asserted "UNCOVERED" and thus
appeared to pass for the wrong reason — which is exactly why the ABOVE case
is the one that proves the ratio is live, not the below case alone (see
module docstring, and the plan's own warning: "A test that only exercises
the below-threshold path cannot tell a working ratio from a constant.").
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import List

import pytest

from coordinator_core import coverage as cov
from coordinator_core import dag


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command in cwd; raise on non-zero exit."""
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
    """dag._FRONTMATTER_CACHE is module-level; clear it so a stale parse from a
    prior test's tmp_path never masks a fresh fixture's frontmatter."""
    dag._FRONTMATTER_CACHE.clear()
    yield
    dag._FRONTMATTER_CACHE.clear()


def _write_trail_record(path: Path, sha: str) -> None:
    """Write a minimal trail record JSON covering exactly `sha` (mirrors
    test_coverage_reviewed_set.py's helper and coverage.py's real
    state/review-trail/*.json schema)."""
    record = {
        "sha_range": f"{sha}^..{sha}",
        "reviewer": "code-reviewer",
        "scope": "session",
        "scope_kind": "diff",
        "verdict": "ok",
        "diff_loc": 1,
        "session_id": "00000000-0000-0000-0000-000000000001",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def _build_three_code_commit_chain(repo: Path) -> tuple[str, List[str]]:
    """A base commit plus 3 real-source commits (distinct file per commit,
    no bookkeeping/planning path involved — each is unambiguously CODE).
    Returns (base_sha, [c1, c2, c3])."""
    repo.mkdir()
    _init_repo(repo)
    (repo / "README.md").write_text("base\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "base commit"], repo)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    shas = []
    for i in range(1, 4):
        (repo / f"module_{i}.py").write_text(f"print({i})\n")
        _git(["add", f"module_{i}.py"], repo)
        _git(["commit", "-m", f"add module_{i}.py"], repo)
        shas.append(_git(["rev-parse", "HEAD"], repo).stdout.strip())
    return base_sha, shas


def test_above_threshold_ratio_is_covered(tmp_path: Path) -> None:
    """2/3 code commits reviewed (~66.7%) >= DEFAULT_COVERAGE_RATIO_THRESHOLD
    (0.66) -> VERDICT=COVERED, with 1 commit still genuinely uncovered. This
    is the case a binary any-uncovered test gets WRONG (pre-C10 it would
    resolve UNCOVERED) and a "return COVERED for everything" bug gets RIGHT
    for the wrong reason — proven distinct from the below-threshold case
    below by an actually-computed ratio, not a hardcoded verdict.
    """
    repo = tmp_path / "repo"
    base_sha, shas = _build_three_code_commit_chain(repo)

    for sha in shas[:2]:
        _write_trail_record(
            repo / "state" / "review-trail" / f"record_{sha[:8]}.json", sha
        )

    result = cov.run_coverage_gate(range_arg=f"{base_sha}..HEAD", repo_root=str(repo))

    assert result.verdict == "COVERED", (
        f"expected COVERED at 2/3 (~0.667) coverage_ratio, at/above the "
        f"{cov.DEFAULT_COVERAGE_RATIO_THRESHOLD} threshold; "
        f"verdict_line={result.verdict_line!r} notes={result.notes!r}"
    )
    assert result.coverage_ratio == pytest.approx(2 / 3), (
        f"expected coverage_ratio == 2/3; got {result.coverage_ratio!r}"
    )
    assert len(result.uncovered_shas) == 1, (
        "the third, unreviewed commit must still be reported as uncovered — "
        "COVERED at threshold is not the same as claiming 100%"
    )
    assert "coverage_ratio=0.67" in result.verdict_line


def test_below_threshold_ratio_is_warn(tmp_path: Path) -> None:
    """1/3 code commits reviewed (~33.3%) < DEFAULT_COVERAGE_RATIO_THRESHOLD
    (0.66) -> VERDICT=WARN, carrying the coordinator:review-code remediation
    offer, never a block. exit_code stays 0 (WARN does not halt) — the
    hard-block decision this chunk names explicitly.
    """
    repo = tmp_path / "repo"
    base_sha, shas = _build_three_code_commit_chain(repo)

    _write_trail_record(
        repo / "state" / "review-trail" / f"record_{shas[0][:8]}.json", shas[0]
    )

    result = cov.run_coverage_gate(range_arg=f"{base_sha}..HEAD", repo_root=str(repo))

    assert result.verdict == "WARN", (
        f"expected WARN at 1/3 (~0.333) coverage_ratio, below the "
        f"{cov.DEFAULT_COVERAGE_RATIO_THRESHOLD} threshold; "
        f"verdict_line={result.verdict_line!r} notes={result.notes!r}"
    )
    assert result.coverage_ratio == pytest.approx(1 / 3)
    assert result.exit_code == 0, "WARN must never halt (this chunk's hard-block decision)"
    assert "coverage_ratio=0.33" in result.verdict_line
    assert any("coordinator:review-code" in n for n in result.notes), (
        f"WARN must carry the remediation OFFER, not just a bare warning; "
        f"notes={result.notes!r}"
    )
    assert not any("bypass" in n.lower() or "override" in n.lower() for n in result.notes), (
        "WARN must offer remediation, never a bypass/override instruction — "
        "this gate has no COORDINATOR_OVERRIDE_* key of its own"
    )
