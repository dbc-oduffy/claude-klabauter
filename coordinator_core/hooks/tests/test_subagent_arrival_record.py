"""Tests for hooks.subagent_arrival_record: direct handler calls under tmp_path."""

from __future__ import annotations

import asyncio
import json

from coordinator_core.arrival_record import arrival_log_path, has_arrived
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks.subagent_arrival_record import _handler


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _run(payload, repo):
    return asyncio.run(_handler(payload, repo))


def _log(repo, session_id):
    return arrival_log_path(repo / ".git", session_id)


def test_background_agent_is_recorded(tmp_path):
    repo = _repo(tmp_path)
    out = _run(
        {"session_id": "sess-1", "agent_id": "a1b2c3d4e5f60718", "agent_type": "coordinator:executor"},
        repo,
    )
    assert out == no_advisory()
    lines = _log(repo, "sess-1").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["agent_id"] == "a1b2c3d4e5f60718"
    assert row["agent_type"] == "coordinator:executor"
    assert row["stopped_at"].endswith("Z")
    assert has_arrived(repo / ".git", "sess-1", "a1b2c3d4e5f60718")


def test_named_teammate_is_recorded(tmp_path):
    repo = _repo(tmp_path)
    out = _run(
        {"session_id": "sess-2", "agent_id": "adana-0123456789abcdef", "agent_type": "coordinator:reviewer"},
        repo,
    )
    assert out == no_advisory()
    assert has_arrived(repo / ".git", "sess-2", "adana-0123456789abcdef")


def test_missing_agent_id_writes_nothing(tmp_path):
    repo = _repo(tmp_path)
    out = _run({"session_id": "sess-3", "agent_type": "x"}, repo)
    assert out == no_advisory()
    assert not _log(repo, "sess-3").exists()
    assert not (repo / ".git" / "coordinator-sessions").exists()


def test_missing_session_id_writes_nothing(tmp_path):
    repo = _repo(tmp_path)
    assert _run({"agent_id": "abc"}, repo) == no_advisory()
    assert not (repo / ".git" / "coordinator-sessions").exists()


def test_unsafe_id_fails_open_and_writes_nothing(tmp_path):
    repo = _repo(tmp_path)
    out = _run({"session_id": "sess-4", "agent_id": "../evil"}, repo)
    assert out == no_advisory()
    assert not _log(repo, "sess-4").exists()


def test_no_repo_root_is_silent():
    assert _run({"session_id": "s", "agent_id": "a"}, None) == no_advisory()
