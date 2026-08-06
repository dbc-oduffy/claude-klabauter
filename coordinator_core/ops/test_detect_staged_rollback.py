"""Tests for coordinator_core.ops.detect_staged_rollback.

All fixtures are real, throwaway git repos built under pytest's `tmp_path` —
never against this repo's own working tree. Each helper commits a small
history and stages content by writing + `git add`, mirroring the exact shape
of the 2026-07-28 staged-rollback incident this module was built to catch.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.ops.detect_staged_rollback import (
    MIN_ROLLBACK_DEPTH,
    MIN_ROLLBACK_PATHS,
    OVERRIDE_ENV,
    find_rollback_candidates,
    main,
)


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(repo):
    repo.mkdir(exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "rollback-test@example.com")
    _git(repo, "config", "user.name", "rollback-test")
    return repo


def _commit_file(repo, name, content, message):
    # Scoped to `-- name`: a bare `git commit` commits the WHOLE index, which
    # would silently sweep up another path's already-staged rollback content
    # (built earlier in the same fixture loop) into an unrelated commit.
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message, "--", name)


def _stage_file(repo, name, content):
    (repo / name).write_text(content)
    _git(repo, "add", name)


def _env(**overrides):
    base = dict(os.environ)
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Clean / trivial cases


def test_clean_index_no_staged_changes(tmp_path):
    repo = _init_repo(tmp_path / "clean")
    _commit_file(repo, "f.txt", "one\n", "c1")
    assert find_rollback_candidates(str(repo)) == []
    assert main([str(repo)], env=_env()) == 0


def test_empty_repo_no_commits_no_crash(tmp_path):
    repo = _init_repo(tmp_path / "empty")
    assert find_rollback_candidates(str(repo)) == []
    assert main([str(repo)], env=_env()) == 0


def test_ordinary_new_staged_content_does_not_fire(tmp_path):
    repo = _init_repo(tmp_path / "ordinary")
    _commit_file(repo, "f.txt", "one\n", "c1")
    _stage_file(repo, "f.txt", "brand new content never committed before\n")
    assert find_rollback_candidates(str(repo)) == []
    assert main([str(repo)], env=_env()) == 0


# ---------------------------------------------------------------------------
# Single-file cases — must NOT fire alone unless deep


def test_single_file_shallow_match_does_not_fire(tmp_path):
    """Staging back to the IMMEDIATELY prior version (depth 1) of one file
    is the ordinary 'undo my last edit' shape and must not fire alone."""
    repo = _init_repo(tmp_path / "shallow")
    _commit_file(repo, "f.txt", "v1\n", "c1")
    _commit_file(repo, "f.txt", "v2\n", "c2")
    _stage_file(repo, "f.txt", "v1\n")

    candidates = find_rollback_candidates(str(repo))
    assert len(candidates) == 1
    assert candidates[0].depth == 1
    assert candidates[0].depth < MIN_ROLLBACK_DEPTH

    assert main([str(repo)], env=_env()) == 0


def test_single_file_deep_match_fires(tmp_path):
    """One file jumping back past >= MIN_ROLLBACK_DEPTH of its own
    intervening commits is the shape the incident actually produced —
    must fire even though only one path is involved."""
    repo = _init_repo(tmp_path / "deep")
    _commit_file(repo, "f.txt", "v1\n", "c1")
    _commit_file(repo, "f.txt", "v2\n", "c2")
    _commit_file(repo, "f.txt", "v3\n", "c3")
    _commit_file(repo, "f.txt", "v4\n", "c4")
    _stage_file(repo, "f.txt", "v1\n")  # 3 commits touching f.txt skipped back

    candidates = find_rollback_candidates(str(repo))
    assert len(candidates) == 1
    assert candidates[0].depth == 3
    assert candidates[0].depth >= MIN_ROLLBACK_DEPTH
    assert candidates[0].matched_subject == "c1"
    assert [s.subject for s in candidates[0].skipped] == ["c4", "c3", "c2"]

    rc = main([str(repo)], env=_env())
    assert rc == 1


# ---------------------------------------------------------------------------
# Multi-file breadth


def test_two_paths_below_breadth_and_shallow_does_not_fire(tmp_path):
    repo = _init_repo(tmp_path / "two-shallow")
    for name in ("a.txt", "b.txt"):
        _commit_file(repo, name, "v1\n", f"{name} c1")
        _commit_file(repo, name, "v2\n", f"{name} c2")
        _stage_file(repo, name, "v1\n")

    candidates = find_rollback_candidates(str(repo))
    assert len(candidates) == 2
    assert len(candidates) < MIN_ROLLBACK_PATHS
    assert all(c.depth < MIN_ROLLBACK_DEPTH for c in candidates)

    assert main([str(repo)], env=_env()) == 0


def test_three_paths_breadth_fires_even_at_shallow_depth(tmp_path):
    """>= MIN_ROLLBACK_PATHS distinct rollback candidates fires regardless
    of how shallow each individual match is — this is the incident's actual
    shape reproduced: nine files, each an exact single-step-back match."""
    repo = _init_repo(tmp_path / "breadth")
    for name in ("a.txt", "b.txt", "c.txt"):
        _commit_file(repo, name, "v1\n", f"{name} c1")
        _commit_file(repo, name, "v2\n", f"{name} c2")
        _stage_file(repo, name, "v1\n")

    candidates = find_rollback_candidates(str(repo))
    assert len(candidates) == 3

    rc = main([str(repo)], env=_env())
    assert rc == 1


def test_incident_shape_nine_paths_mixed_depth_fires(tmp_path):
    """Reproduces the 2026-07-28 incident's actual shape: many files, each
    staged as an exact match to some older commit, at varying depths — the
    real event this detector exists to catch."""
    repo = _init_repo(tmp_path / "incident")
    for i in range(9):
        name = f"file{i}.py"
        # Each file gets a history depth of (i % 4) + 2 commits, so depths
        # vary across the staged set the way the real incident's nine files
        # each matched a different point in their own history.
        depth = (i % 4) + 1
        for v in range(depth + 1):
            _commit_file(repo, name, f"v{v}\n", f"{name} c{v}")
        _stage_file(repo, name, "v0\n")

    candidates = find_rollback_candidates(str(repo))
    assert len(candidates) == 9

    rc = main([str(repo)], env=_env())
    assert rc == 1


# ---------------------------------------------------------------------------
# Override


def test_override_env_exits_zero_but_still_prints_findings(tmp_path, capsys):
    repo = _init_repo(tmp_path / "override")
    for name in ("a.txt", "b.txt", "c.txt"):
        _commit_file(repo, name, "v1\n", f"{name} c1")
        _commit_file(repo, name, "v2\n", f"{name} c2")
        _stage_file(repo, name, "v1\n")

    rc = main([str(repo)], env=_env(**{OVERRIDE_ENV: "1"}))
    assert rc == 0

    captured = capsys.readouterr()
    assert "a.txt" in captured.err
    assert "b.txt" in captured.err
    assert "c.txt" in captured.err
    assert OVERRIDE_ENV in captured.err


def test_override_env_zero_value_still_blocks(tmp_path):
    """An explicit "0" is treated as not-set — mirrors the sibling gates'
    override convention (a truthy env var, not merely 'the var exists')."""
    repo = _init_repo(tmp_path / "override-zero")
    for name in ("a.txt", "b.txt", "c.txt"):
        _commit_file(repo, name, "v1\n", f"{name} c1")
        _commit_file(repo, name, "v2\n", f"{name} c2")
        _stage_file(repo, name, "v1\n")

    rc = main([str(repo)], env=_env(**{OVERRIDE_ENV: "0"}))
    assert rc == 1


# ---------------------------------------------------------------------------
# Report content


def test_report_names_paths_matched_commit_and_undone_subjects(tmp_path, capsys):
    repo = _init_repo(tmp_path / "report")
    _commit_file(repo, "f.txt", "v1\n", "original work")
    _commit_file(repo, "f.txt", "v2\n", "second edit")
    _commit_file(repo, "f.txt", "v3\n", "third edit")
    _stage_file(repo, "f.txt", "v1\n")

    rc = main([str(repo)], env=_env())
    assert rc == 1

    captured = capsys.readouterr()
    assert "f.txt" in captured.err
    assert "original work" in captured.err
    assert "second edit" in captured.err
    assert "third edit" in captured.err


# ---------------------------------------------------------------------------
# Argv handling


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_usage_and_exits_clean(flag, capsys):
    """Regression: `--help` was taken as a repo-root path, so the CLI died on
    `FileNotFoundError: '--help'` — a traceback where a usage block belongs.
    Asserted on `main` rather than the trampoline because that is where the
    handling lives; the bareword CLI forwards argv verbatim."""
    rc = main([flag], env=_env())

    assert rc == 0
    assert "usage: detect-staged-rollback" in capsys.readouterr().out


def test_unknown_option_is_a_usage_error_not_a_repo_root(capsys):
    rc = main(["--bogus"], env=_env())

    assert rc == 2
    captured = capsys.readouterr()
    assert "--bogus" in captured.err
    assert "usage: detect-staged-rollback" in captured.err
    assert captured.out == ""

