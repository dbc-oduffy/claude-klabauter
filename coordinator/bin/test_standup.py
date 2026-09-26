from __future__ import annotations

import os
import subprocess
import sys

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(SCRIPT_DIR, "standup.py")
PYTHON = sys.executable


def _make_git_repo(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        r = subprocess.run(cmd, cwd=path, capture_output=True, text=True, **no_console_creationflags())
        if r.returncode != 0:
            raise RuntimeError(f"FATAL: git setup failed in {path}: {r.stderr}")


def _run_helper(cwd):
    r = subprocess.run(
        [PYTHON, HELPER],
        cwd=cwd,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    return r.returncode, r.stdout, r.stderr


def test_no_bash_literal_in_source():
    with open(HELPER, "r", encoding="utf-8") as fh:
        source = fh.read()
    assert '"bash"' not in source and "'bash'" not in source


def test_not_a_git_repo_hard_errors(tmp_path):
    nogit_dir = tmp_path / "nogit"
    nogit_dir.mkdir()
    code, so, se = _run_helper(str(nogit_dir))
    assert code == 1
    assert "not inside a git repository" in se


def test_fresh_git_repo_emits_all_sections(tmp_path):
    repo = tmp_path / "repo"
    _make_git_repo(str(repo))
    code, so, se = _run_helper(str(repo))
    assert code == 0
    assert "> Baseline:" in so
    assert "== Commits today ==" in so
    assert "== Files changed by dir ==" in so
    assert "== Handoffs touched today ==" in so
    assert "== Todo files touched today ==" in so
    assert "== Active handoffs ==" in so
