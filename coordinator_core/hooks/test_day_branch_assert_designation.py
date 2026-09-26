
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.daily_branch import read_configured_day_branch
from coordinator_core.hooks.day_branch_assert import COMPLIANT, WARN, assert_day_branch
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process


def _run(argv):
    subprocess.run(argv, check=True, **no_console_creationflags())


def _init_repo(tmp_path, branch: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", str(repo)])
    _run(["git", "-C", str(repo), "checkout", "-q", "-b", branch])
    _run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"])
    return repo


def test_designated_branch_already_configured_is_compliant_silent(tmp_path):
    repo = _init_repo(tmp_path, "claude/compassionate-pascal-98ncw7")
    _run(["git", "-C", str(repo), "config", "coordinator.dayBranch",
          "claude/compassionate-pascal-98ncw7"])
    result = assert_day_branch(str(repo), "machine-a", "2026-09-22")
    assert result.outcome == COMPLIANT
    assert result.message == ""


def test_cloud_session_learns_and_records_designation(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path, "claude/compassionate-pascal-98ncw7")
    assert read_configured_day_branch(repo) is None

    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    result = assert_day_branch(str(repo), "machine-a", "2026-09-22")

    assert result.outcome == COMPLIANT
    assert read_configured_day_branch(repo) == "claude/compassionate-pascal-98ncw7"


def test_non_cloud_session_does_not_learn_a_designation(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path, "some-workstream-branch")
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)

    result = assert_day_branch(str(repo), "machine-a", "2026-09-22")

    assert read_configured_day_branch(repo) is None
    assert result.outcome == WARN


def test_configured_branch_survives_a_second_boot_without_the_cloud_env(tmp_path, monkeypatch):
    """Once learned, the designation is honoured even if a later boot in
    the same tree does not carry CLAUDE_CODE_REMOTE (e.g. a shell
    re-invoking the assert directly) -- the record on disk is the source,
    not the env var, once written."""
    repo = _init_repo(tmp_path, "claude/compassionate-pascal-98ncw7")
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    assert_day_branch(str(repo), "machine-a", "2026-09-22")

    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    result = assert_day_branch(str(repo), "machine-a", "2026-09-22")
    assert result.outcome == COMPLIANT
