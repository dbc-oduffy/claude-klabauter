"""test_assert_cwd — pytest tests for coordinator/bin/assert-cwd.py.

Spec backlink: scratchpad/scout-D-claude-klabauter-sizing.md § Item 2 (new-project
cwd-assert, example-doctrine-repo new-project/SKILL.md:76).

Subprocess-driven (not in-process import): assert-cwd's whole contract is
"read the CALLING process's cwd via `git rev-parse --show-toplevel`" -- an
in-process call can't vary that without chdir'ing the test runner itself
(unsafe under parallel test execution), so each test spawns the real script
with an explicit `cwd=` instead.

Coverage:
    test_match_exits_zero
    test_mismatch_exits_one_with_stderr_message
    test_not_a_git_worktree_exits_one
    test_wrong_arg_count_exits_two
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parent.parent / "assert-cwd.py"


def _run(args, cwd=None):
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_match_exits_zero(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    cp = _run([str(repo)], cwd=str(repo))

    assert cp.returncode == 0, cp.stderr


def test_mismatch_exits_one_with_stderr_message(tmp_path):
    repo = tmp_path / "repo2"
    _init_git_repo(repo)
    other = tmp_path / "not-the-repo"
    other.mkdir()

    cp = _run([str(other)], cwd=str(repo))

    assert cp.returncode == 1
    assert "ERROR: cwd is" in cp.stderr
    assert str(other.resolve()) in cp.stderr


def test_not_a_git_worktree_exits_one(tmp_path):
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()

    cp = _run([str(plain_dir)], cwd=str(plain_dir))

    assert cp.returncode == 1
    assert "not inside a git working tree" in cp.stderr


def test_wrong_arg_count_exits_two(tmp_path):
    cp = _run([], cwd=str(tmp_path))
    assert cp.returncode == 2

    cp2 = _run(["a", "b"], cwd=str(tmp_path))
    assert cp2.returncode == 2
