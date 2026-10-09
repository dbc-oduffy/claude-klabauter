"""The spawning half of test_context_pressure_precompact: `_build_git_section` read
against a real git repo, so its fast-tier sibling keeps every non-spawning test."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.hooks import context_pressure_precompact as cpp

# _git builds the fixture repo with real git processes.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_build_git_section_reads_repo_without_spawning(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "feature")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first subject\n\nbody")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")

    def _no_spawn(*a, **k):
        raise AssertionError("subprocess.Popen called")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    lines = cpp._build_git_section(str(repo))

    assert lines[:4] == ["", "## Git State", "Branch: feature", "Recent commits:"]
    assert lines[4].endswith(" first subject") and len(lines[4].split()[0]) == 10
    assert lines[5:] == [
        "",
        "Modified files:",
        cpp._UNSTAGED_NOT_LISTED,
        "Staged files:",
        "b.txt",
    ]
