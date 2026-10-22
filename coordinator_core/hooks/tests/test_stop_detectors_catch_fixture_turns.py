"""Pins that the Stop detectors `hooks.guard_turn_intent` and `hooks.guard_manufactured_blocker`
fire on the turns they exist to catch: a final turn announcing work with no tool call, and a
final turn claiming to be blocked. Fixtures are built under tmp_path; the payload shape is the
one hook-run pipes (strict level, stop_hook_active false, no role marker, no Cockpit signal)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from coordinator_core import machine_profile
from coordinator_core.hooks import guard_manufactured_blocker as gmb
from coordinator_core.hooks import guard_turn_intent as gti

ANNOUNCE_TEXT = "I'll now run the full fast test suite and report the results."
BLOCKED_TEXT = "I'm blocked and waiting before I can go any further."


@pytest.fixture(autouse=True)
def _strict_level(monkeypatch):
    monkeypatch.setattr(machine_profile, "guard_level", lambda *_a, **_k: "strict")
    monkeypatch.delenv("COORDINATOR_COCKPIT", raising=False)


def _stop_payload(tmp_path: Path, text: str) -> dict:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    transcript = tmp_path / "probe.jsonl"
    entries = [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}},
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return {
        "session_id": str(uuid.uuid4()),
        "cwd": str(root),
        "transcript_path": str(transcript),
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }


def test_turn_intent_blocks_announced_but_unexecuted_final_turn(tmp_path):
    result = gti._handler({"payload": _stop_payload(tmp_path, ANNOUNCE_TEXT)})
    assert result.get("decision") == "block", result


def test_manufactured_blocker_advises_on_claimed_blocked_final_turn(tmp_path):
    result = gmb._handler({"payload": _stop_payload(tmp_path, BLOCKED_TEXT)})
    assert result, result
