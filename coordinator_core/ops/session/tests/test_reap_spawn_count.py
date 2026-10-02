"""Exact-equality spawn count for `session.reap`'s dirty-touched-path refusal.

A stale agent dir carrying touched paths costs one whole-tree `git status --porcelain`,
paid once per reap however many agent dirs qualify. The figure is read from the budget
manifest, never chosen here.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops.session import reap
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo, *args):
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )


def _budget() -> dict:
    return load_manifest()["overrides"]["session.reap"]["spawn_count_budget"]


def _stale_agent_with_touched_path(sessions, aid: str) -> None:
    adir = sessions / ".agents" / aid
    adir.mkdir(parents=True)
    (adir / "em-session-id.txt").write_text("sid-dead\n", encoding="utf-8")
    touch_record.append_event(
        adir / "touch-record.jsonl",
        session_id="sid-dead",
        agent_id=None,
        verb=touch_record.VERB_TOUCH,
        path="src/foo.py",
    )
    when = time.time() - (reap._AGENT_STALE_SECONDS + 3600)
    for member in adir.iterdir():
        os.utime(member, (when, when))
    os.utime(adir, (when, when))


def test_two_stale_agents_with_touched_paths_spawn_one_status_in_total(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    sessions = repo / ".git" / "coordinator-sessions"
    sessions.mkdir()
    _stale_agent_with_touched_path(sessions, "agent-a")
    _stale_agent_with_touched_path(sessions, "agent-b")
    monkeypatch.setattr(reap, "resolve_live_session_ids", lambda: frozenset())

    with _count_spawns_attributed(monkeypatch) as spawns:
        result = asyncio.run(reap._handler({"force": True}, repo_root=repo / ".git"))

    assert result["exit_code"] == 0, result
    assert sorted(result["reaped_agents"]) == ["agent-a", "agent-b"], result
    assert len(spawns) == _budget()["dirty_touched_path_refusal"], [s.argv for s in spawns]
    assert [s.origin for s in spawns] == ["_git._invoke"] * len(spawns)
    assert all("status" in s.argv for s in spawns), [s.argv for s in spawns]
