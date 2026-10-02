"""Exact-equality spawn count for `hooks.stop_dispatch`'s one git read.

The terminal-review leg walks this session's commits with one `git log`; that is the
only process the Stop fan-in starts. The figure is read from the budget manifest, never
chosen here, so a second spawn on the Stop path fails this test instead of fitting under it.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.hooks import stop_dispatch
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo, *args):
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )


def _budget() -> dict:
    return load_manifest()["overrides"]["hooks.stop_dispatch"]["spawn_count_budget"]


def test_stop_with_a_session_commit_spawns_exactly_the_budgeted_git_log(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "work\n\nSession-Id: sess-stop-count")

    payload = {"session_id": "sess-stop-count", "cwd": str(repo)}
    with _count_spawns_attributed(monkeypatch) as spawns:
        asyncio.run(stop_dispatch._handler({"payload": payload}))

    assert len(spawns) == _budget()["terminal_review_session_walk"], [s.argv for s in spawns]
    assert [s.origin for s in spawns] == ["run_git"] * len(spawns)
    assert all("log" in s.argv for s in spawns), [s.argv for s in spawns]
