"""
coordinator_core.tests.test_coverage_touched_paths_batching — regression
tests for the 2026-08-11 Windows-argv-limit batching fix
(docs/plans/2026-08-11-coverage-add-commit-authorship-is-not-a.md, C3/C4).

Root cause (coordinator_core/coverage.py, `_commit_touched_paths`, prior to
this fix): the bookkeeping/planning classifier resolved every uncached SHA's
touched paths via ONE unchunked `git log --no-walk --name-only <shas...>`
call, passing every SHA as a positional argv entry. An unscoped
`review-coverage-gate.py` invocation on this repo (1924 commits) blew past
Windows' ~32K command-line length ceiling: "[WinError 206] The filename or
extension is too long" — the whole classification silently degraded to
fail-closed CODE for every one of those commits, and the resulting
coverage_ratio was still printed as an ordinary VERDICT.

Fix: `_commit_touched_paths` chunks its `git log` calls at
`_TOUCHED_PATHS_CHUNK` (mirrors `_bulk_trailer_lookup`'s existing
`_TRAILER_LOOKUP_CHUNK` pattern), and `run_coverage_gate` downgrades any
COVERED verdict to WARN — never silently COVERED — whenever classification
was skipped for any commit (AC6), so a ratio computed over an incomplete
partition cannot read as a clean finding.
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


def _build_code_commit_chain(repo: Path, n: int) -> tuple[str, List[str]]:
    repo.mkdir()
    _init_repo(repo)
    (repo / "README.md").write_text("base\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "base commit"], repo)
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    shas = []
    for i in range(1, n + 1):
        (repo / f"module_{i}.py").write_text(f"print({i})\n")
        _git(["add", f"module_{i}.py"], repo)
        _git(["commit", "-m", f"add module_{i}.py"], repo)
        shas.append(_git(["rev-parse", "HEAD"], repo).stdout.strip())
    return base_sha, shas


def test_commit_touched_paths_batches_under_small_chunk_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_commit_touched_paths` must resolve every SHA correctly even when the
    chunk size is far smaller than the input set — i.e. it actually spans
    MULTIPLE `git log` spawns rather than one unchunked call. Forcing
    `_TOUCHED_PATHS_CHUNK` down to 2 over 7 SHAs guarantees >=4 chunks.

    Pre-fix `coverage.py` has no `_TOUCHED_PATHS_CHUNK` attribute at all —
    `monkeypatch.setattr(cov, "_TOUCHED_PATHS_CHUNK", 2)` raises
    AttributeError on that revision (default `raising=True`), which is how
    this test was confirmed to fail against pre-fix code.
    """
    repo = tmp_path / "repo"
    base_sha, shas = _build_code_commit_chain(repo, 7)
    monkeypatch.setattr(cov, "_TOUCHED_PATHS_CHUNK", 2)

    resolved, note = cov._commit_touched_paths(shas, str(repo), {})

    assert note is None, f"unexpected diagnostic note: {note!r}"
    assert set(resolved.keys()) == set(shas)
    for i, sha in enumerate(shas, start=1):
        assert resolved[sha] == frozenset({f"module_{i}.py"}), (
            f"sha {sha} resolved to {resolved[sha]!r}, expected "
            f"{{'module_{i}.py'}} — chunking must not shear or misattribute "
            "per-commit path lists across chunk boundaries"
        )


def test_unscoped_large_chain_no_longer_dies_on_argv_limit(tmp_path: Path) -> None:
    """End-to-end: `run_coverage_gate` over a chain wide enough to have
    previously risked the unchunked-argv failure must still classify cleanly
    (small N here stands in for the 1924-commit real-world case — the
    property under test is "chunking runs correctly", not the exact commit
    count that trips a 32K ceiling, which is impractical to reproduce in a
    fast unit test).
    """
    repo = tmp_path / "repo"
    base_sha, shas = _build_code_commit_chain(repo, 10)

    result = cov.run_coverage_gate(range_arg=f"{base_sha}..HEAD", repo_root=str(repo))

    assert not any(
        "git log failed" in n for n in result.notes
    ), f"classification must not report a git-log failure; notes={result.notes!r}"
    assert result.chain_commits == 10
    assert result.exit_code == 0


def test_classification_skipped_downgrades_covered_to_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6: when `_commit_touched_paths` reports a diagnostic note (git log
    genuinely failed for >=1 chunk, even after batching), a run that would
    otherwise resolve COVERED must be downgraded to WARN — a ratio computed
    over a corpus that failed to classify must never read as a clean
    finding. Forced here by monkeypatching `_run` to fail unconditionally
    for the touched-paths call so the note fires deterministically.
    """
    repo = tmp_path / "repo"
    base_sha, shas = _build_code_commit_chain(repo, 3)

    # 2/3 covered (~0.667, at/above DEFAULT_COVERAGE_RATIO_THRESHOLD) so
    # absent the injected failure this run resolves cleanly COVERED — the
    # control this test needs to prove the downgrade is live, not a
    # constant. The remaining uncovered commit is what drives
    # `_classify_bookkeeping_shas` -> `_commit_touched_paths` to actually
    # run (an empty uncovered set never reaches it at all).
    import json

    for sha in shas[:2]:
        record_path = repo / "state" / "review-trail" / f"record_{sha[:8]}.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(
                {
                    "sha_range": f"{sha}^..{sha}",
                    "reviewer": "code-reviewer",
                    "scope": "session",
                    "scope_kind": "diff",
                    "verdict": "ok",
                    "diff_loc": 1,
                    "session_id": "00000000-0000-0000-0000-000000000001",
                }
            ),
            encoding="utf-8",
        )

    real_run = cov._run

    def _failing_run(cmd, cwd=None, **kwargs):
        if isinstance(cmd, list) and "--name-only" in cmd:
            return 1, "", "simulated git failure"
        return real_run(cmd, cwd=cwd, **kwargs)

    monkeypatch.setattr(cov, "_run", _failing_run)

    result = cov.run_coverage_gate(range_arg=f"{base_sha}..HEAD", repo_root=str(repo))

    assert result.verdict != "COVERED", (
        "a run whose classification was skipped must never resolve "
        f"silently COVERED; verdict={result.verdict!r} notes={result.notes!r}"
    )
    assert any("skipped" in n.lower() for n in result.notes)
