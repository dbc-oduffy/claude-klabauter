"""Unit tests for `coordinator_core.ops.memo.surface_advisory`.

Spec: docs/plans/2026-09-11-realized-by-vs-declared-surface-reconcile.md (P080-C1).

Falsifier-first (per the plan's Test surface section): `test_instance_3_partial_realization`
below is written before `surface_advisory.py` existed, encodes the instance-3 fixture (AC7,
first sentence), and is the falsifier this chunk demonstrates red under a scratch mutation
that forces `untouched_declared` to `[]` (see the chunk's dispatch report for the recorded
red output -- the mutation itself is never committed).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.ops.memo.surface_advisory import (
    declared_surface,
    surface_advisory,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ENV_EXTRA = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    import os
    env = {**os.environ, **_ENV_EXTRA}
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check, capture_output=True, text=True, env=env,
        **no_console_creationflags(),
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "commit.gpgsign", "false", cwd=path)
    return path


def _commit(repo: Path, files: dict[str, str], message: str = "c") -> str:
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        _git("add", "--", rel, cwd=repo)
    _git("commit", "-q", "-m", message, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def test_instance_3_partial_realization(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, {"README.md": "base"}, "base")
    sha = _commit(
        repo,
        {
            "coordinator_core/ops/ceremony/scoped_git_commit.py": "x",
            "claims.py": "y",
        },
        "realize",
    )
    frontmatter = {
        "scoped_to": {
            "artifact": [
                "coordinator/bin/session-claim-cli",
                "coordinator_core/ops/ceremony/scoped_git_commit.py",
            ]
        },
        "realized_by": sha,
    }
    result = surface_advisory(frontmatter, repo)
    assert result["verdict"] == "ok"
    assert result["untouched_declared"] == ["coordinator/bin/session-claim-cli"]


class TestDeclaredSurface:
    def test_scoped_to_artifact_scalar(self):
        assert declared_surface({"scoped_to": {"artifact": "foo/bar.py"}}) == ["foo/bar.py"]

    def test_scoped_to_artifact_list(self):
        assert declared_surface({"scoped_to": {"artifact": ["a/b.py", "c/d.py"]}}) == [
            "a/b.py", "c/d.py",
        ]

    def test_top_level_surface_scalar(self):
        assert declared_surface({"surface": "x/y.py"}) == ["x/y.py"]

    def test_top_level_surface_list(self):
        assert declared_surface({"surface": ["x/y.py", "z.py"]}) == ["x/y.py", "z.py"]

    def test_scoped_to_takes_precedence_over_surface(self):
        fm = {"scoped_to": {"artifact": "a.py"}, "surface": "b.py"}
        assert declared_surface(fm) == ["a.py"]

    def test_empty_scoped_to_falls_back_to_surface(self):
        fm = {"scoped_to": {"artifact": []}, "surface": "b.py"}
        assert declared_surface(fm) == ["b.py"]

    def test_neither_present_returns_empty(self):
        assert declared_surface({}) == []

    def test_normalization_backticks_space_symbol_dotslash_trailing_slash_backslash(self):
        fm = {
            "surface": [
                "`foo/bar.py` -- the file",
                "./baz/qux.py::MyClass",
                "dir/sub/",
                "win\\style\\path.py",
            ]
        }
        assert declared_surface(fm) == [
            "foo/bar.py",
            "baz/qux.py",
            "dir/sub",
            "win/style/path.py",
        ]


class TestNoneCases:
    def test_absent_realized_by(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            assert surface_advisory({"surface": "a.py"}, tmp_path) is None
            mock_run.assert_not_called()

    def test_non_hex_realized_by(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            assert surface_advisory(
                {"surface": "a.py", "realized_by": "not-a-sha"}, tmp_path
            ) is None
            mock_run.assert_not_called()

    def test_scientific_notation_shaped_realized_by(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            assert surface_advisory(
                {"surface": "a.py", "realized_by": "7.17e385"}, tmp_path
            ) is None
            mock_run.assert_not_called()

    def test_short_hex_below_floor_is_none(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            assert surface_advisory(
                {"surface": "a.py", "realized_by": "abc123"}, tmp_path
            ) is None
            mock_run.assert_not_called()

    def test_uppercase_sha_is_lowercased_and_accepted(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        sha = _commit(repo, {"a.py": "x"}, "c")
        result = surface_advisory({"surface": "a.py", "realized_by": sha.upper()}, repo)
        assert result is not None
        assert result["sha"] == sha.lower()


def test_no_declared_surface_zero_spawns(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    sha = _commit(repo, {"a.py": "x"}, "c")
    with patch("subprocess.run") as mock_run:
        result = surface_advisory({"realized_by": sha}, repo)
    assert result == {
        "verdict": "no-declared-surface",
        "sha": sha,
        "declared": [],
        "untouched_declared": [],
    }
    mock_run.assert_not_called()


def test_one_spawn_for_ok_verdict(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    sha = _commit(repo, {"a.py": "x"}, "c")
    real_run = subprocess.run
    calls = []

    def _counting_run(*args, **kwargs):
        calls.append((args, kwargs))
        return real_run(*args, **kwargs)

    with patch("subprocess.run", side_effect=_counting_run):
        result = surface_advisory({"surface": "a.py", "realized_by": sha}, repo)
    assert result["verdict"] == "ok"
    assert len(calls) == 1
    argv = calls[0][0][0]
    assert argv[:2] == ["git", "log"]
    assert "--root" in argv
    assert "--diff-merges=first-parent" in argv
    assert "--name-only" in argv
    assert "--format=" in argv
    assert argv[-1] == f"{sha}^{{commit}}"


class TestGitFixtures:
    def test_root_commit_full_tree(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        sha = _commit(repo, {"a.py": "x", "b.py": "y"}, "root")
        result = surface_advisory({"surface": "a.py", "realized_by": sha}, repo)
        assert result["verdict"] == "ok"
        assert result["untouched_declared"] == []

    def test_merge_commit_first_parent_diff(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        _commit(repo, {"base.txt": "base"}, "base")
        _git("checkout", "-b", "feature", cwd=repo)
        _commit(repo, {"feature.py": "f"}, "feature commit")
        _git("checkout", "-", cwd=repo)
        _git(
            "merge", "--no-ff", "-m", "merge feature", "feature", cwd=repo,
        )
        merge_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        result = surface_advisory({"surface": "feature.py", "realized_by": merge_sha}, repo)
        assert result["verdict"] == "ok"

    def test_empty_commit_is_paper_realization(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        _commit(repo, {"real_file.py": "x"}, "base")
        _git("commit", "--allow-empty", "-q", "-m", "empty", cwd=repo)
        sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        result = surface_advisory({"surface": "real_file.py", "realized_by": sha}, repo)
        assert result["verdict"] == "paper-realization"

    def test_ambiguous_or_failing_resolution_is_unresolved_not_paper(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        _commit(repo, {"real_file.py": "x"}, "base")
        real_run = subprocess.run

        def _fail_run(*args, **kwargs):
            cp = real_run(*args, **kwargs)
            cp.returncode = 128
            cp.stdout = ""
            return cp

        with patch("subprocess.run", side_effect=_fail_run):
            result = surface_advisory(
                {"surface": "real_file.py", "realized_by": "abcdef0"}, repo
            )
        assert result["verdict"] == "unresolved-sha"
        assert result["reason"] == "not-a-commit-here"

    def test_tree_object_sha_is_unresolved(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        _commit(repo, {"real_file.py": "x"}, "base")
        tree_sha = _git("rev-parse", "HEAD^{tree}", cwd=repo).stdout.strip()
        result = surface_advisory({"surface": "real_file.py", "realized_by": tree_sha}, repo)
        assert result["verdict"] == "unresolved-sha"
        assert result["reason"] == "not-a-commit-here"

    def test_blob_object_sha_is_unresolved(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        _commit(repo, {"real_file.py": "x"}, "base")
        blob_sha = _git(
            "hash-object", str(repo / "real_file.py"), cwd=repo
        ).stdout.strip()
        result = surface_advisory({"surface": "real_file.py", "realized_by": blob_sha}, repo)
        assert result["verdict"] == "unresolved-sha"
        assert result["reason"] == "not-a-commit-here"

    def test_timeout_is_advisory_failed(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        sha = _commit(repo, {"real_file.py": "x"}, "base")
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=3),
        ):
            result = surface_advisory({"surface": "real_file.py", "realized_by": sha}, repo)
        assert result["verdict"] == "unresolved-sha"
        assert result["reason"] == "advisory-failed"

    def test_unexpected_exception_is_advisory_failed(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        sha = _commit(repo, {"real_file.py": "x"}, "base")
        with patch("subprocess.run", side_effect=RuntimeError("boom")):
            result = surface_advisory({"surface": "real_file.py", "realized_by": sha}, repo)
        assert result["verdict"] == "unresolved-sha"
        assert result["reason"] == "advisory-failed"


class TestVerdictPrecedence:
    def test_no_declared_surface_over_unresolved_sha(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        with patch("subprocess.run") as mock_run:
            result = surface_advisory({"realized_by": "abcdef0"}, repo)
        assert result["verdict"] == "no-declared-surface"
        mock_run.assert_not_called()

    def test_unresolved_sha_over_ok(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        _commit(repo, {"a.py": "x"}, "c")
        result = surface_advisory({"surface": "a.py", "realized_by": "abcdef0"}, repo)
        assert result["verdict"] == "unresolved-sha"

    def test_ok_over_foreign_surface_when_one_of_several_touched(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        sha = _commit(repo, {"a.py": "x"}, "c")
        result = surface_advisory({"surface": ["a.py", "ghost/never.py"], "realized_by": sha}, repo)
        assert result["verdict"] == "ok"
        assert result["untouched_declared"] == ["ghost/never.py"]

    def test_foreign_surface_over_paper_realization(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        sha = _commit(repo, {"docs/plans/x.md": "x"}, "c")
        result = surface_advisory(
            {"surface": "ghost/does-not-exist.py", "realized_by": sha}, repo
        )
        assert result["verdict"] == "foreign-surface"

    def test_paper_realization_over_out_of_surface(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        _commit(repo, {"real_file.py": "x"}, "base")
        sha = _commit(repo, {"docs/plans/x.md": "y"}, "planning")
        result = surface_advisory({"surface": "real_file.py", "realized_by": sha}, repo)
        assert result["verdict"] == "paper-realization"

    def test_out_of_surface_when_declared_exists_but_untouched_and_touched_is_code(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        _commit(repo, {"real_file.py": "x"}, "base")
        sha = _commit(repo, {"docs/plans/x.md": "y", "other.py": "z"}, "mixed")
        result = surface_advisory({"surface": "real_file.py", "realized_by": sha}, repo)
        assert result["verdict"] == "out-of-surface"

    def test_one_module_over_case_is_out_of_surface(self, tmp_path):
        repo = _init_repo(tmp_path / "repo")
        _commit(repo, {"pickup_assemble/apply.py": "x"}, "base")
        sha = _commit(repo, {"pickup_assemble/__init__.py": "y"}, "moved")
        result = surface_advisory(
            {"surface": "pickup_assemble/apply.py", "realized_by": sha}, repo
        )
        assert result["verdict"] == "out-of-surface"


def test_coverage_import_is_function_local(tmp_path):
    sys.modules.pop("coordinator_core.coverage", None)
    repo = _init_repo(tmp_path / "repo")
    sha = _commit(repo, {"real_file.py": "x"}, "base")
    result = surface_advisory({"surface": "real_file.py", "realized_by": sha}, repo)
    assert result["verdict"] == "ok"
    assert "coordinator_core.coverage" not in sys.modules

    sha2 = _commit(repo, {"docs/plans/x.md": "y"}, "planning")
    result2 = surface_advisory({"surface": "real_file.py", "realized_by": sha2}, repo)
    assert result2["verdict"] == "paper-realization"
    assert "coordinator_core.coverage" in sys.modules


def test_module_has_no_top_level_coverage_import():
    import coordinator_core.ops.memo.surface_advisory as mod
    import inspect

    source = inspect.getsource(mod)
    code_region = source.split("\ndef ", 1)[1] if "\ndef " in source else source
    top_level_lines = [
        line for line in code_region.splitlines() if not line.startswith((" ", "\t"))
    ]
    assert not any("coverage" in line for line in top_level_lines)
