"""Pins the Stop channel each box-memory guard uses, driven through the production handler.

A Stop hook reaches the MODEL only as top-level {"decision": "block", "reason": ...} on stdout
(exit 0, nothing on stderr). A top-level systemMessage reaches the user only.
"""

from __future__ import annotations

import json
import uuid

import pytest

from coordinator_core.hooks import guard_manufactured_blocker as gmb
from coordinator_core.hooks import guard_turn_intent as gti


def _git_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _transcript(tmp_path, text):
    path = tmp_path / "t.jsonl"
    entry = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return str(path)


def _assert_model_visible_block(envelope):
    assert envelope.get("decision") == "block", envelope
    assert envelope.get("reason"), envelope
    assert "hookSpecificOutput" not in envelope
    assert "systemMessage" not in envelope
    json.dumps(envelope)


def test_manufactured_blocker_altitude_block_is_stop_decision_block(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(gmb, "resolve_posture", lambda _r: "default")
    monkeypatch.setattr(gmb, "apply_guard_level", lambda _n, env: env)
    monkeypatch.setattr(gmb, "_auto_discharge_fire", lambda _p: None)
    payload = {
        "session_id": "sess-" + uuid.uuid4().hex,
        "cwd": str(repo),
        "transcript_path": _transcript(tmp_path, "Two things need you. Please decide."),
    }
    _assert_model_visible_block(gmb._handler({"payload": payload}))


def test_manufactured_blocker_declarative_stall_is_user_only_systemmessage(tmp_path):
    payload = {
        "session_id": "sess-" + uuid.uuid4().hex,
        "cwd": str(_git_repo(tmp_path)),
        "transcript_path": _transcript(tmp_path, "I'm blocked and waiting before I can go any further."),
    }
    envelope = gmb._handler({"payload": payload})
    assert "systemMessage" in envelope, envelope
    assert "decision" not in envelope and "hookSpecificOutput" not in envelope


def test_turn_intent_announce_block_is_stop_decision_block(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    monkeypatch.setenv("COORDINATOR_COCKPIT", "1")
    monkeypatch.setattr(gti, "apply_guard_level", lambda _n, env: env)
    payload = {
        "session_id": str(uuid.uuid4()),
        "cwd": str(repo),
        "last_assistant_message": "I'll now run the full fast test suite and report the results.",
    }
    _assert_model_visible_block(gti._handler({"payload": payload}))


def test_turn_intent_outside_cockpit_does_not_block(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_COCKPIT", raising=False)
    monkeypatch.setattr(gti, "apply_guard_level", lambda _n, env: env)
    payload = {
        "session_id": str(uuid.uuid4()),
        "cwd": str(_git_repo(tmp_path)),
        "last_assistant_message": "I'll now run the full fast test suite and report the results.",
    }
    assert gti._handler({"payload": payload}) == {}
