"""Exact-equality spawn count for `hooks.postuse_advisory_dispatch`.

The Bash-commit claim-release leg compares the index to HEAD and starts no process. A Write
whose path the zero-spawn normalizer cannot prove (non-ASCII) pays one `git ls-files` in the
touch-record leg. The figures are read from the budget manifest, never chosen here.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.hooks import postuse_advisory_dispatch as pad
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo, *args):
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )


def _budget() -> dict:
    return load_manifest()["overrides"]["hooks.postuse_advisory_dispatch"]["spawn_count_budget"]


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    return root


def _fire(repo, monkeypatch, params):
    monkeypatch.setenv("CLAUDE_HOME", str(repo.parent / "claude-home"))
    with _count_spawns_attributed(monkeypatch) as spawns:
        asyncio.run(pad._handler(params, repo_root=repo / ".git"))
    return spawns


def test_ascii_write_touch_spawns_nothing(repo, monkeypatch):
    (repo / "plain.py").write_text("x = 1\n", encoding="utf-8")
    spawns = _fire(
        repo,
        monkeypatch,
        {"session_id": "sid-ascii", "tool_name": "Write", "file_path": str(repo / "plain.py")},
    )
    assert len(spawns) == _budget()["ascii_write_touch"], [s.argv for s in spawns]


def test_non_ascii_write_touch_spawns_exactly_the_budgeted_ls_files(repo, monkeypatch):
    (repo / "café.py").write_text("x = 1\n", encoding="utf-8")
    spawns = _fire(
        repo,
        monkeypatch,
        {"session_id": "sid-nonascii", "tool_name": "Write", "file_path": str(repo / "café.py")},
    )
    assert len(spawns) == _budget()["non_ascii_write_touch"], [s.argv for s in spawns]
    assert [s.origin for s in spawns] == ["run_git"] * len(spawns)
    assert all("ls-files" in s.argv for s in spawns), [s.argv for s in spawns]
