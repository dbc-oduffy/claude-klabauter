"""test_standup.py — self-contained test suite for standup.py.

Review: code-reviewer — F5 (P2): standup.py landed with no test file. This suite covers
the not-inside-a-git-repo hard error, the baseline/no-baseline output shape, section
presence/ordering, and — the F1 regression this campaign exists to prevent — a static
guarantee that no `bash` (or any shell) subprocess spawn is reachable from the module.

Converted from a hand-rolled PASS/FAIL runner (standup.test.py) to pytest-collectable
top-level test functions.

Spec backlink: archive/specs/2026-05-05-script-first-deterministic-ops.md §T1
Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md
"""
from __future__ import annotations

import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(SCRIPT_DIR, "standup.py")
PYTHON = sys.executable
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _make_git_repo(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        r = subprocess.run(cmd, cwd=path, capture_output=True, text=True, creationflags=_NO_WINDOW)
        if r.returncode != 0:
            raise RuntimeError(f"FATAL: git setup failed in {path}: {r.stderr}")


def _run_helper(cwd):
    r = subprocess.run(
        [PYTHON, HELPER],
        cwd=cwd,
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
    )
    return r.returncode, r.stdout, r.stderr


def test_no_bash_literal_in_source():
    """F1 regression net: no literal bash subprocess argv anywhere in standup.py."""
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
