"""A declared directory never reaches measured_rev: the stage's reviewable list holds files only."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit import measured_rev as mr

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root, *args):
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "bin" / "tests").mkdir(parents=True)
    (tmp_path / "bin" / "tool.py").write_text("x = 1\n")
    (tmp_path / "bin" / "tests" / "test_tool.py").write_text("def test_a():\n    pass\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


DECLARED = ["bin", "bin/tool.py", "bin/tests/test_tool.py"]


def test_reviewable_drops_a_declared_directory_when_rooted(repo):
    assert ask_stage._reviewable(DECLARED, repo) == ("bin/tool.py", "bin/tests/test_tool.py")


def test_reviewable_without_root_keeps_every_non_excluded_path(repo):
    assert ask_stage._reviewable(DECLARED) == tuple(DECLARED)


def test_reviewable_still_drops_excluded_prefix(repo):
    assert ask_stage._reviewable(["scratch/x.py", "bin/tool.py"], repo) == ("bin/tool.py",)


def test_measured_rev_measures_the_filtered_list(repo):
    out = mr.measured_rev("run-dir", list(ask_stage._reviewable(DECLARED, repo)), repo_root=repo)
    assert "bin/tool.py" in out["paths"]


def test_measured_rev_still_refuses_a_directory_in_paths(repo):
    with pytest.raises(mr.MeasuredRevRefused):
        mr.measured_rev("run-dir", DECLARED, repo_root=repo)
