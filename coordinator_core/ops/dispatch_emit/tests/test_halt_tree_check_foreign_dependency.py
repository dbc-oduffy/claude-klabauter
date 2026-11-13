"""halt_tree_check.broken_paths: an import miss caused by a peer's uncommitted module is not the run's defect."""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import halt_tree_check

pytestmark = pytest.mark.spawns_process


def _git_repo(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", *cmd], cwd=repo, env=env, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return repo


def test_untracked_foreign_dependency_does_not_break_own_file(tmp_path):
    repo = _git_repo(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "x = 1\n"})
    (repo / "pkg/dep.py").write_text("def helper():\n    pass\n")
    (repo / "pkg/a.py").write_text("from pkg.dep import helper\nx = 2\n")
    assert halt_tree_check.broken_paths(repo, ["pkg/a.py"], ["pkg/a.py"]) == {}


def test_name_only_an_uncommitted_edit_of_tracked_dependency_defines_is_not_broken(tmp_path):
    repo = _git_repo(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "x = 1\n", "pkg/dep.py": "old = 1\n"})
    (repo / "pkg/dep.py").write_text("old = 1\ndef helper():\n    pass\n")
    (repo / "pkg/a.py").write_text("from pkg.dep import helper\nx = 2\n")
    assert halt_tree_check.broken_paths(repo, ["pkg/a.py"], ["pkg/a.py"]) == {}


def test_name_defined_nowhere_is_still_broken(tmp_path):
    repo = _git_repo(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "x = 1\n", "pkg/dep.py": "old = 1\n"})
    (repo / "pkg/dep.py").write_text("old = 2\n")
    (repo / "pkg/a.py").write_text("from pkg.dep import undefined_name\nx = 2\n")
    own = ["pkg/a.py", "pkg/dep.py"]
    assert "pkg/a.py" in halt_tree_check.broken_paths(repo, own, own)


def test_own_dependency_that_is_evicted_keeps_importer_broken(tmp_path):
    repo = _git_repo(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "x = 1\n"})
    (repo / "pkg/dep.py").write_text("def (:\n")
    (repo / "pkg/a.py").write_text("from pkg.dep import helper\nx = 2\n")
    out = halt_tree_check.broken_paths(repo, ["pkg/a.py", "pkg/dep.py"], ["pkg/a.py", "pkg/dep.py"])
    assert "syntax error" in out["pkg/dep.py"]
    assert "pkg/a.py" in out


def test_syntax_error_in_own_file_still_breaks_despite_foreign_dependency(tmp_path):
    repo = _git_repo(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "x = 1\n"})
    (repo / "pkg/dep.py").write_text("def helper():\n    pass\n")
    (repo / "pkg/a.py").write_text("from pkg.dep import helper\ndef (:\n")
    assert "syntax error" in halt_tree_check.broken_paths(repo, ["pkg/a.py"], ["pkg/a.py"])["pkg/a.py"]


def test_owner_suffix_is_stripped_before_matching():
    detail = "imports helper from pkg.dep; pkg/dep.py is uncommitted in the worktree; owned by live run ask-1"
    assert halt_tree_check._foreign_uncommitted_target(detail, ["pkg/a.py"])
    assert not halt_tree_check._foreign_uncommitted_target(detail, ["pkg/dep.py"])
    assert not halt_tree_check._foreign_uncommitted_target("imports x; absent from HEAD", [])
