"""Directory pathspecs in a repo no peer can share; the many-files recipe runs and is allowed."""
from __future__ import annotations

import os
import re
import subprocess

import pytest

from coordinator_core.bash_guards import dispatch_checks as guard
from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    )


@pytest.fixture
def fresh_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, **no_console_passthrough_kwargs())
    return tmp_path


def test_fresh_repo_has_no_peer(fresh_repo):
    assert guard._bt_no_peer_can_share_the_index(str(fresh_repo), "s1")


def test_committed_repo_with_only_own_session_has_no_peer(fresh_repo):
    (fresh_repo / "a.txt").write_text("a")
    _git(fresh_repo, "add", "a.txt")
    _git(fresh_repo, "commit", "-qm", "init")
    (fresh_repo / ".git" / "coordinator-sessions" / "s1").mkdir(parents=True)
    assert guard._bt_no_peer_can_share_the_index(str(fresh_repo), "s1")


def test_a_second_session_dir_means_a_peer_may_share_the_index(fresh_repo):
    (fresh_repo / "a.txt").write_text("a")
    _git(fresh_repo, "add", "a.txt")
    _git(fresh_repo, "commit", "-qm", "init")
    sessions = fresh_repo / ".git" / "coordinator-sessions"
    (sessions / "s1").mkdir(parents=True)
    (sessions / "s2").mkdir()
    assert not guard._bt_no_peer_can_share_the_index(str(fresh_repo), "s1")


def test_subtree_foreign_paths_empty_in_a_fresh_repo(fresh_repo):
    (fresh_repo / "docs").mkdir()
    (fresh_repo / "docs" / "x.md").write_text("x")
    assert guard._bt_add_subtree_foreign_paths(
        str(fresh_repo / "docs"), str(fresh_repo), str(fresh_repo), "s1"
    ) == []


def test_many_files_recipe_runs_and_the_guard_allows_it(fresh_repo, monkeypatch):
    monkeypatch.setattr(guard, "_is_hazard_repo", lambda root: True)
    (fresh_repo / "docs").mkdir()
    for n in range(3):
        (fresh_repo / "docs" / ("f%d.md" % n)).write_text("x")
    monkeypatch.chdir(fresh_repo)
    denied = guard.check_blanket_git_add("git add .", "s1")
    reason = denied["hookSpecificOutput"]["permissionDecisionReason"]
    steps = re.findall(r"^  (git (?:ls-files|add --pathspec|commit -m <subject> --pathspec)[^\n]*)$", reason, re.M)
    assert len(steps) == 3
    env = {**os.environ, "TMPDIR": str(fresh_repo / ".git")}
    for step in steps:
        step = step.replace("<dir>", "docs").replace("<subject>", "'init'")
        step = step.replace("git ", "git -c user.name=t -c user.email=t@t ", 1)
        assert guard.check_blanket_git_add(step, "s1") is None
        subprocess.run(["bash", "-c", step], cwd=fresh_repo, env=env, check=True, capture_output=True, **no_console_creationflags())
    assert "docs/f0.md" in _git(fresh_repo, "ls-files").stdout
