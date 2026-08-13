"""
coordinator_core.ops.ceremony.tests.test_scoped_git_commit_untracked_omitted

Tests for the 2026-08-12 fix (state/bug-backlog/2026-08-12-scoped-git-commit-
silently-omits-untracked-files-in-a-pathspec.yaml): `ceremony.scoped_git_commit`
never STAGES an untracked file beneath a directory pathspec element (unchanged,
protective, and not under test here) but must now SAY SO in its response
(`untracked_paths_omitted`) rather than staying silent about the gap.

Coverage:
  - a directory pathspec with untracked content beneath it: reported, and the
    untracked file is still NOT staged/committed.
  - a directory pathspec with no untracked content: no report, response
    otherwise unchanged (byte-identical key set to before this fix).
  - a gitignored file beneath the directory: never reported (git status
    --porcelain already excludes it; not an omission).
  - the cap: more than `_UNTRACKED_OMITTED_CAP` untracked files beneath the
    directory truncates the sample and reports the true `count`.

`--repo <other-worktree>` is a CLI-layer flag (`coordinator/bin/scoped-git-
commit`'s `_worktree_root`), not an op-boundary concept -- `_call` below
always passes `worktree_root` explicitly, so there is no cwd-vs-explicit
distinction here for it to exercise. Its real coverage lives in
`coordinator/bin/tests/test_scoped_git_commit_cli.py`.

All git operations run against a throwaway repo created fresh under
`tmp_path` — never the working repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.ops.ceremony import scoped_git_commit


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _committed_files_at_head(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _call(params: dict) -> dict:
    return scoped_git_commit._handler(params, repo_root=None)


def test_directory_pathspec_with_untracked_content_is_reported_and_not_staged(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "pkg/tracked.txt", "seed")
    _git(["add", "--", "pkg/tracked.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "pkg/tracked.txt", "modified tracked content")
    _seed_file(repo, "pkg/fresh_module.py", "new module")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["pkg"],
            "message": "update pkg",
        }
    )

    assert result["committed"] is True
    committed_files = _committed_files_at_head(repo)
    assert committed_files == ["pkg/tracked.txt"]
    assert "pkg/fresh_module.py" not in committed_files

    info = result["untracked_paths_omitted"]
    assert info["count"] == 1
    assert info["paths"] == ["pkg/fresh_module.py"]
    assert info["truncated"] is False


def test_directory_pathspec_with_no_untracked_content_has_no_report(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "pkg/tracked.txt", "seed")
    _git(["add", "--", "pkg/tracked.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "pkg/tracked.txt", "modified tracked content")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["pkg"],
            "message": "update pkg, nothing untracked",
        }
    )

    assert result["committed"] is True
    assert "untracked_paths_omitted" not in result


def test_gitignored_file_beneath_directory_is_never_reported(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, ".gitignore", "__pycache__/\n*.pyc\n")
    _seed_file(repo, "pkg/tracked.txt", "seed")
    _git(["add", "--", ".gitignore", "pkg/tracked.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "pkg/tracked.txt", "modified tracked content")
    _seed_file(repo, "pkg/__pycache__/tracked.cpython-311.pyc", "bytecode")

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["pkg"],
            "message": "update pkg, ignored content present",
        }
    )

    assert result["committed"] is True
    assert "untracked_paths_omitted" not in result


# Review: code-reviewer -- Finding [P2 nit], 2026-08-12. This module used to
# carry a `test_repo_form_matches_in_repo_form` here, claiming to cover the
# CLI's `--repo <other-worktree>` form. It never did: `_call` (above) invokes
# `scoped_git_commit._handler` DIRECTLY, and the op's own `worktree_root`
# param is always explicit -- there is no cwd-vs-explicit-root distinction at
# the op boundary for it to exercise. It created an unused `other_cwd`, never
# passed it to `_call` or `_git`, and was byte-for-byte identical to
# `test_directory_pathspec_with_untracked_content_is_reported_and_not_staged`
# above under a misleading name. Deleted, not renamed: the `--repo` flag is a
# CLI-layer concept (`coordinator/bin/scoped-git-commit`'s `_worktree_root`),
# so its real coverage now lives at that layer instead --
# `coordinator/bin/tests/test_scoped_git_commit_cli.py::TestUntrackedOmittedEndToEnd::
# test_repo_flag_from_another_cwd_reports_untracked_sibling`, which runs the
# real CLI subprocess from a DIFFERENT cwd with `--repo <repo>` pointed at
# the scratch repo -- genuinely exercising the resolution path this test's
# name promised.


def test_untracked_sample_is_capped_and_reports_true_count(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "pkg/tracked.txt", "seed")
    _git(["add", "--", "pkg/tracked.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "pkg/tracked.txt", "modified tracked content")
    for i in range(25):
        _seed_file(repo, "pkg/fresh_%02d.py" % i, "new module %d" % i)

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["pkg"],
            "message": "update pkg with many untracked siblings",
        }
    )

    assert result["committed"] is True
    info = result["untracked_paths_omitted"]
    assert info["count"] == 25
    assert len(info["paths"]) == scoped_git_commit._UNTRACKED_OMITTED_CAP
    assert info["truncated"] is True
