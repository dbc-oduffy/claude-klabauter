"""
Tests for coordinator_core.ops.staleness_git.

Uses real throwaway git fixture repos (tmp_path + `git init`/`git commit`) —
this module is git plumbing, and mocking `git` would test the mock rather
than the plumbing.

Spec backlink: docs/plans/2026-08-13-generator-output-staleness-detector.md § C0
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from coordinator_core.ops.staleness_git import (
    SinceRange,
    Verdict,
    commits_touching_since,
    git_root,
    verdict_from_range,
)
from coordinator_core.win_portability import no_console_creationflags

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test")


def _commit(repo: Path, message: str, files: dict[str, str], *, when: str | None = None) -> str:
    """Commit *files*. *when* (RFC-2822 or ISO, e.g. "2020-01-01T00:00:00")
    pins both author and committer dates so equivalence tests can force a
    clean second-granularity gap between commits — two real-clock commits
    issued back-to-back in the same test can otherwise land in the same
    git-timestamp second, which is a timing artifact of the test harness,
    not a case this module needs to handle specially."""
    for rel_path, content in files.items():
        target = repo / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _run(repo, "add", rel_path)
    env = None
    if when is not None:
        import os

        env = dict(os.environ)
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    result = subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, f"git commit failed: {result.stderr}"
    head = _run(repo, "rev-parse", "HEAD")
    return head.stdout.strip()


def _one_second_after(iso_timestamp: str) -> str:
    """`git log --since=<T>` is inclusive of a commit dated exactly T, while
    `<sha>..HEAD` strictly excludes that commit's own SHA — the two forms
    are only equivalent starting one second after a boundary commit's own
    timestamp. Used to translate a commit-ish's timestamp into the
    "immediately after this commit" instant a caller migrating between
    forms would supply."""
    return (datetime.fromisoformat(iso_timestamp) + timedelta(seconds=1)).isoformat()


def test_git_root_resolves_from_nested_cwd(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "initial", {"a.txt": "1"})

    nested = repo / "a" / "b" / "c"
    nested.mkdir(parents=True)

    root = git_root(cwd=str(nested))

    assert root is not None
    assert root.resolve() == repo.resolve()


def test_git_root_none_outside_repo(tmp_path):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    assert git_root(cwd=str(outside)) is None


def test_git_failure_yields_indeterminate_not_raise(tmp_path):
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()

    rng = commits_touching_since(non_repo, ["src.py"], "2020-01-01T00:00:00Z")

    assert rng.indeterminate is True
    assert rng.commits == ()
    assert rng.detail
    assert verdict_from_range(rng) == Verdict.INDETERMINATE


def test_unresolvable_since_point_is_indeterminate_never_fresh(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "initial", {"src.py": "1"})

    rng = commits_touching_since(repo, ["src.py"], "not-a-timestamp-or-sha")

    # Plain `git log --since=<garbage>` would silently ignore an
    # unparseable date and match every commit (a masked false-STALE, the
    # ALWAYS-0-adjacent failure mode this module exists to close) — the
    # module must reject the shape itself rather than trust git's leniency.
    assert rng.indeterminate is True
    assert verdict_from_range(rng) == Verdict.INDETERMINATE


def test_empty_since_range_distinguishable_from_failed_range(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "initial", {"src.py": "1"})

    far_future = "2099-01-01T00:00:00"
    empty_rng = commits_touching_since(repo, ["src.py"], far_future)
    assert empty_rng.commits == ()
    assert empty_rng.indeterminate is False
    assert verdict_from_range(empty_rng) == Verdict.FRESH

    non_repo = repo / "src.py"  # not a directory -> forces failure path
    failed_rng = commits_touching_since(non_repo, ["src.py"], far_future)
    assert failed_rng.commits == ()
    assert failed_rng.indeterminate is True
    assert verdict_from_range(failed_rng) == Verdict.INDETERMINATE

    assert empty_rng != failed_rng


def test_timestamp_and_commit_ish_since_point_agree(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _commit(repo, "initial", {"src.py": "1"}, when="2020-01-01T00:00:00")

    # capture the ISO timestamp of the base commit
    show = _run(repo, "show", "-s", "--format=%cI", base_sha)
    base_timestamp = show.stdout.strip()

    _commit(repo, "touch src", {"src.py": "2"}, when="2020-01-01T00:00:05")

    rng_by_sha = commits_touching_since(repo, ["src.py"], base_sha)
    rng_by_timestamp = commits_touching_since(repo, ["src.py"], _one_second_after(base_timestamp))

    assert verdict_from_range(rng_by_sha) == Verdict.STALE
    assert verdict_from_range(rng_by_timestamp) == Verdict.STALE
    assert len(rng_by_sha.commits) == len(rng_by_timestamp.commits) == 1


def test_timestamp_and_commit_ish_agree_on_fresh(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _commit(repo, "initial", {"src.py": "1", "other.py": "1"}, when="2020-01-01T00:00:00")
    show = _run(repo, "show", "-s", "--format=%cI", base_sha)
    base_timestamp = show.stdout.strip()

    # subsequent commit touches an unrelated file only
    _commit(repo, "touch other", {"other.py": "2"}, when="2020-01-01T00:00:05")

    rng_by_sha = commits_touching_since(repo, ["src.py"], base_sha)
    rng_by_timestamp = commits_touching_since(repo, ["src.py"], _one_second_after(base_timestamp))

    assert verdict_from_range(rng_by_sha) == Verdict.FRESH
    assert verdict_from_range(rng_by_timestamp) == Verdict.FRESH


def test_artifact_path_excludes_regeneration_commit_sha_form(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _commit(repo, "initial", {"src.py": "1", "artifact.json": "1"})

    # single commit that touches BOTH sources and the artifact -- a
    # regeneration, must be excluded from the since-range
    _commit(repo, "fix and regen", {"src.py": "2", "artifact.json": "2"})

    rng = commits_touching_since(
        repo, ["src.py"], base_sha, artifact_path="artifact.json"
    )

    assert rng.indeterminate is False
    assert rng.commits == ()
    assert verdict_from_range(rng) == Verdict.FRESH


def test_artifact_path_excludes_regeneration_commit_timestamp_form(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _commit(repo, "initial", {"src.py": "1", "artifact.json": "1"}, when="2020-01-01T00:00:00")
    show = _run(repo, "show", "-s", "--format=%cI", base_sha)
    base_timestamp = show.stdout.strip()

    _commit(repo, "fix and regen", {"src.py": "2", "artifact.json": "2"}, when="2020-01-01T00:00:05")

    rng = commits_touching_since(
        repo, ["src.py"], _one_second_after(base_timestamp), artifact_path="artifact.json"
    )

    assert rng.indeterminate is False
    assert rng.commits == ()
    assert verdict_from_range(rng) == Verdict.FRESH


def test_artifact_path_excludes_root_commit_regeneration_timestamp_form(tmp_path):
    # Review: coordinator:code-reviewer — `git diff-tree <commit>` without
    # `--root` never reports a parentless commit as touching anything (it
    # diffs against nothing rather than the empty tree), so a root commit
    # that touches BOTH sources and the artifact must still be excluded by
    # the regeneration filter once `--root` is present.
    repo = tmp_path / "repo"
    _init_repo(repo)
    root_sha = _commit(
        repo, "root touches both", {"src.py": "1", "artifact.json": "1"}, when="2020-01-01T00:00:00"
    )

    rng = commits_touching_since(
        repo, ["src.py"], "1970-01-01T00:00:00", artifact_path="artifact.json"
    )

    assert rng.indeterminate is False
    assert root_sha not in rng.commits
    assert rng.commits == ()
    assert verdict_from_range(rng) == Verdict.FRESH


def test_artifact_path_excludes_root_commit_regeneration_commit_ish_form(tmp_path):
    # Same as above but with a commit-ish `since_point` that is not an
    # ancestor of the root commit under test -- forces `<since>..HEAD` to
    # include the root commit itself in the query, exercising `--root` via
    # the SHA-form comparison path rather than `--since=`.
    repo = tmp_path / "repo"
    _init_repo(repo)
    default_branch = _run(repo, "branch", "--show-current").stdout.strip()
    root_sha = _commit(repo, "root touches both", {"src.py": "1", "artifact.json": "1"})

    _run(repo, "checkout", "--orphan", "other-root")
    _run(repo, "rm", "-rf", "--cached", ".")
    _run(repo, "clean", "-fd")
    other_root_sha = _commit(repo, "unrelated other root", {"unrelated.txt": "1"})
    _run(repo, "checkout", default_branch)

    rng = commits_touching_since(
        repo, ["src.py"], other_root_sha, artifact_path="artifact.json"
    )

    assert rng.indeterminate is False
    assert root_sha not in rng.commits
    assert rng.commits == ()
    assert verdict_from_range(rng) == Verdict.FRESH


def test_artifact_path_batch_handles_mixed_multi_commit_range(tmp_path):
    # Multi-item angle on the batched `_commits_touching_path` replacement
    # for the old per-commit `git diff-tree` loop -- a single-commit fixture
    # would pass identically whether or not the batch call correctly
    # attributes each SHA to its own touch result (the same gap that shipped
    # a wrong batched `_own_frozen_diff_shas` on 2026-08-19). Three commits:
    # one drift-only (must stay), one regeneration touching both (must be
    # excluded), one drift-only again (must stay) -- exercises correct
    # per-commit attribution across a batch, not just "some filtering
    # happened".
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _commit(repo, "initial", {"src.py": "1", "artifact.json": "1"})
    drift_one = _commit(repo, "drift only 1", {"src.py": "2"})
    _commit(repo, "regen", {"src.py": "3", "artifact.json": "2"})
    drift_two = _commit(repo, "drift only 2", {"src.py": "4"})

    rng = commits_touching_since(
        repo, ["src.py"], base_sha, artifact_path="artifact.json"
    )

    assert rng.indeterminate is False
    assert set(rng.commits) == {drift_one, drift_two}
    assert verdict_from_range(rng) == Verdict.STALE


def test_artifact_path_does_not_exclude_source_only_commit(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    base_sha = _commit(repo, "initial", {"src.py": "1", "artifact.json": "1"})

    # commit touches sources but NOT the artifact -- genuine drift, must stay
    drift_sha = _commit(repo, "drift only", {"src.py": "2"})

    rng = commits_touching_since(
        repo, ["src.py"], base_sha, artifact_path="artifact.json"
    )

    assert rng.commits == (drift_sha,)
    assert verdict_from_range(rng) == Verdict.STALE
