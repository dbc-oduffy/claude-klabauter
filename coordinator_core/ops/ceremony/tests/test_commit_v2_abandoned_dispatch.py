"""
coordinator_core.ops.ceremony.tests.test_commit_v2_abandoned_dispatch

Purpose: a dispatch that timed out must not see its commit land afterwards. The dispatcher
sets `dispatch_abandon.ACTIVE`'s event on timeout; `commit_paths` checks it before the ref update.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "--", "README.md"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _call(repo: Path) -> dict:
    return commit_v2._handler(
        {"paths": ["a.txt"], "message": "add a\n",
         "session_id": "11111111-2222-4333-8444-555555555555"},
        repo_root=repo / ".git",
    )


def test_abandoned_dispatch_leaves_head_untouched(repo):
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    head = _git(["rev-parse", "HEAD"], repo)
    ev = threading.Event()
    ev.set()
    token = dispatch_abandon.ACTIVE.set(ev)
    try:
        result = _call(repo)
    finally:
        dispatch_abandon.ACTIVE.reset(token)

    assert result["committed"] is False
    assert "timed out" in result["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_live_dispatch_still_lands(repo):
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    token = dispatch_abandon.ACTIVE.set(threading.Event())
    try:
        result = _call(repo)
    finally:
        dispatch_abandon.ACTIVE.reset(token)

    assert result["committed"] is True
    assert _git(["rev-parse", "HEAD"], repo) == result["sha"]


def test_dispatcher_timeout_marks_the_handler_thread_abandoned(monkeypatch):
    import asyncio
    import time

    import coordinator_core.ipc as ipc

    seen = {}
    done = threading.Event()

    def _slow(params, repo_root=None):
        time.sleep(0.3)
        seen["abandoned"] = dispatch_abandon.is_abandoned()
        done.set()
        return {}

    monkeypatch.setitem(ipc._REGISTRY, "test.slow_abandon", _slow)
    monkeypatch.setattr(ipc, "_timeout_for", lambda method, msg=None: 0.05)

    asyncio.run(ipc.dispatch_message(
        {"jsonrpc": "2.0", "id": 1, "method": "test.slow_abandon", "params": {}}
    ))

    assert done.wait(2)
    assert seen["abandoned"] is True
