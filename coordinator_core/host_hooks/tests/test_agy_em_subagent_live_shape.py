"""Agy EM/subagent split through `shim.decide`: a hook payload carrying `agentId` (or `agent_id`) is a subagent and
draws the subagent guards; one without is the EM and does not. The engine dial is pinned down (`shim._budgeted`
returns no verdict) so the real in-process guard chain answers.
"""
from __future__ import annotations

import sqlite3
import subprocess

import pytest

from coordinator_core.host_hooks import agy_adapter, shim

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SUBAGENT_ID = "7c1f0e52-3a4b-4d6e-9f10-2b8a5c3d4e60"
_CONVERSATION = "1efd6565-4b6a-434a-989d-f7814c28bdd8"
_SUBAGENT_GUARD = "block-subagent-destructive-action"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.fixture(autouse=True)
def cold_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(shim, "_budgeted", lambda request, budget: ({}, False))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path / "warm"))
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.setattr(agy_adapter, "conversations_dir", lambda: tmp_path / "no-agy-store")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "owned"
    path.mkdir()
    for args in (("init", "-q", "-b", "main"),
                 ("remote", "add", "origin", "https://github.com/me/thing.git")):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, creationflags=_NO_WINDOW)
    (path / "untracked.txt").write_text("u", encoding="utf-8")
    return path


def _payload(repo, command: str, **extra) -> dict:
    return {
        "conversationId": _CONVERSATION,
        "workspacePaths": [str(repo)],
        "toolCall": {"name": "run_command", "args": {"CommandLine": command}},
        "stepIdx": 3,
        **extra,
    }


@pytest.mark.parametrize("key", ["agentId", "agent_id"])
def test_agy_subagent_payload_draws_the_subagent_guard(repo, key):
    answer = shim.decide("agy", _payload(repo, "git clean -fdx", **{key: _SUBAGENT_ID}))
    assert answer["decision"] == "deny"
    assert _SUBAGENT_GUARD in answer["reason"]


def _agy_store(tmp_path, monkeypatch, conversation: str, gen_row: bytes | None) -> None:
    """A conversation db shaped like agy's: `gen_metadata` rows; a child's first row names `parent_cascade_id`."""
    folder = tmp_path / "agy-conversations"
    folder.mkdir(exist_ok=True)
    monkeypatch.setattr(agy_adapter, "conversations_dir", lambda: folder)
    con = sqlite3.connect(folder / f"{conversation}.db")
    con.execute("CREATE TABLE gen_metadata (idx integer, data blob, size integer NOT NULL DEFAULT 0, PRIMARY KEY (idx))")
    if gen_row is not None:
        con.execute("INSERT INTO gen_metadata (idx, data) VALUES (0, ?)", (gen_row,))
    con.commit()
    con.close()


_CHILD_ROW = b"\xa2\x019\n\x11parent_cascade_id\x12$0118f35a-a163-4e60-bd11-56e0e3b29497\xa2\x01\x14\n\x0bused_claude"
_EM_ROW = b"\xa2\x01\x14\n\x0bused_claude\x12\x05false"


def test_agy_child_conversation_without_agent_id_draws_the_subagent_guard(repo, tmp_path, monkeypatch):
    _agy_store(tmp_path, monkeypatch, _CONVERSATION, _CHILD_ROW)
    answer = shim.decide("agy", _payload(repo, "git clean -fdx"))
    assert answer["decision"] == "deny"
    assert _SUBAGENT_GUARD in answer["reason"]


def test_agy_child_conversation_with_no_workspace_in_payload_still_draws_the_subagent_guard(repo, tmp_path, monkeypatch):
    _agy_store(tmp_path, monkeypatch, _CONVERSATION, _CHILD_ROW)
    payload = _payload(repo, "git clean -fdx")
    del payload["workspacePaths"]
    payload["cwd"] = str(repo)
    answer = shim.decide("agy", payload)
    assert answer["decision"] == "deny"


@pytest.mark.parametrize("row", [_EM_ROW, None], ids=["em-row", "no-rows"])
def test_agy_em_conversation_in_the_store_is_not_a_subagent(repo, tmp_path, monkeypatch, row):
    _agy_store(tmp_path, monkeypatch, _CONVERSATION, row)
    answer = shim.decide("agy", _payload(repo, "git reset --hard"))
    assert answer.get("decision") != "deny"
    assert _SUBAGENT_GUARD not in str(answer)


def test_agy_unreadable_store_reads_as_em_never_denies(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(agy_adapter, "conversations_dir", lambda: tmp_path / "absent")
    assert agy_adapter.is_subagent_conversation(_CONVERSATION) is False
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / f"{_CONVERSATION}.db").write_text("not sqlite", encoding="utf-8")
    monkeypatch.setattr(agy_adapter, "conversations_dir", lambda: tmp_path / "bad")
    assert agy_adapter.is_subagent_conversation(_CONVERSATION) is False


@pytest.mark.parametrize("extra", [{}, {"agentId": ""}, {"agentId": None}], ids=["absent", "empty", "null"])
def test_agy_em_payload_does_not_draw_the_subagent_guard(repo, extra):
    answer = shim.decide("agy", _payload(repo, "git reset --hard", **extra))
    assert answer.get("decision") != "deny"
    assert _SUBAGENT_GUARD not in str(answer)
