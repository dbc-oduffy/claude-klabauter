"""Pins the `hooks.guard_turn_intent` op (the EM turn-intent Stop step) and the
`hooks.support.turn_intent` contract module. All state lives under tmp_path fake repos."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from coordinator_core.hooks import guard_turn_intent as gti
from coordinator_core.hooks.support import turn_intent as ti
from coordinator_core.hooks.support.cockpit_session import under_cockpit

TAIL = '<turn-intent>{"needs_pm":true,"ask":"Ship v2 or hold?"}</turn-intent>'
NO_TAIL = "Ran the migration and all checks passed."


# ---- contract module -------------------------------------------------------
def classify(_text):
    return (True, "classified")


def tail(obj):
    return f"done.\n<turn-intent>{obj}</turn-intent>\n"


def test_constants():
    assert ti.SCHEMA == 1 and ti.BLOCK_CAP == 2 and ti.ASK_MAX == 200
    assert ti.RECORD_NAME == "turn-intent.json"


def test_parse_valid_marker_with_trailing_whitespace():
    m = ti.parse_marker('x <turn-intent>{"needs_pm":true,"ask":"pick"}</turn-intent>  \n\n')
    assert m == ti.Marker(True, "pick", "marker")


def test_mid_message_tag_is_not_a_marker():
    assert ti.parse_marker('<turn-intent>{"needs_pm":true,"ask":"a"}</turn-intent> then more') is None
    assert ti.parse_marker("no tag") is None


def test_malformed_and_non_bool():
    assert ti.parse_marker(tail("{nope")).source == "marker-malformed"
    assert ti.parse_marker(tail('{"needs_pm":"yes","ask":""}')).source == "marker-malformed"
    assert ti.parse_marker(tail("[1]")).source == "marker-malformed"
    assert ti.parse_marker(tail('{"needs_pm":true,"ask":5}')).source == "marker-malformed"


def test_overlong_ask_truncated():
    m = ti.parse_marker(tail(json.dumps({"needs_pm": True, "ask": "a" * 500})))
    assert len(m.ask) == ti.ASK_MAX


def test_strip_marker():
    assert ti.strip_marker(tail('{"needs_pm":false,"ask":""}')) == "done."
    assert ti.strip_marker("plain") == "plain"


def test_decide_marker_records_without_block():
    d = ti.decide(tail('{"needs_pm":false,"ask":""}'), 0, True, classify)
    assert d == ti.Decision(False, (False, "", "marker"))


def test_decide_malformed_uses_classifier_never_blocks():
    d = ti.decide(tail("{bad"), 0, True, classify)
    assert d == ti.Decision(False, (True, "classified", "marker-malformed"))


def test_decide_missing_blocks_until_cap_then_heuristic():
    assert ti.decide("hi", 0, True, classify) == ti.Decision(True, None)
    assert ti.decide("hi", 1, True, classify) == ti.Decision(True, None)
    assert ti.decide("hi", 2, True, classify) == ti.Decision(False, (True, "classified", "heuristic"))


def test_decide_no_role_marker_never_blocks():
    assert ti.decide("hi", 0, False, classify) == ti.Decision(False, (True, "classified", "heuristic"))


def test_session_dir_and_role_marker(tmp_path):
    sid = str(uuid.uuid4())
    d = ti.session_dir(str(tmp_path), sid)
    assert not ti.role_marker_present(d)
    assert ti.write_role_marker(d, sid)
    assert ti.role_marker_present(d)


def test_counter_bump_and_reset_on_record(tmp_path):
    sid = str(uuid.uuid4())
    d = ti.session_dir(str(tmp_path), sid)
    os.makedirs(d)
    assert ti.bump_blocks(d) == 1
    assert ti.bump_blocks(d) == 2
    assert ti.write_record(d, sid, True, "q", "marker", 1.0)
    assert ti.read_blocks(d) == 0


def test_write_record_schema_keys_and_no_temp_left(tmp_path):
    sid = str(uuid.uuid4())
    d = ti.session_dir(str(tmp_path), sid)
    assert ti.write_record(d, sid, True, "x" * 300, "heuristic", 12.5)
    rec = json.load(open(os.path.join(d, ti.RECORD_NAME), encoding="utf-8"))
    assert set(rec) == {"schema", "session_id", "needs_pm", "ask", "source", "turn_ended_at"}
    assert rec["schema"] == 1 and rec["session_id"] == sid and rec["turn_ended_at"] == 12.5
    assert len(rec["ask"]) == ti.ASK_MAX
    assert not [n for n in os.listdir(d) if n.endswith(".tmp")]


def test_non_uuid_session_contract_writes_nothing(tmp_path):
    d = ti.session_dir(str(tmp_path), "not-a-uuid")
    assert ti.write_record(d, "not-a-uuid", True, "", "marker", 1.0) is False
    assert ti.write_role_marker(d, "not-a-uuid") is False
    assert not os.path.exists(os.path.join(str(tmp_path), "coordinator-sessions"))


# ---- the op ----------------------------------------------------------------

def _seed(tmp_path: Path, text: str, *, role: bool = True):
    root = tmp_path / "repo"
    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    sid = str(uuid.uuid4())
    transcript = tmp_path / "t.jsonl"
    entry = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    transcript.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    directory = ti.session_dir(str(git_dir), sid)
    if role:
        assert ti.write_role_marker(directory, sid)
    payload = {"session_id": sid, "cwd": str(root), "transcript_path": str(transcript)}
    return root, git_dir, sid, Path(directory), payload


def _run(payload: dict):
    return gti._handler({"payload": payload})


def _blocked(result: dict) -> bool:
    out = result.get("hookSpecificOutput") or {}
    return ti.BLOCK_REASON in str(out.get("permissionDecisionReason", ""))


def _record(directory: Path) -> dict:
    return json.loads((directory / ti.RECORD_NAME).read_text(encoding="utf-8"))


def test_declared_tail_records_marker_without_block(tmp_path):
    _r, _g, sid, d, payload = _seed(tmp_path, f"Done.\n{TAIL}")
    assert not _blocked(_run(payload))
    rec = _record(d)
    assert (rec["source"], rec["needs_pm"], rec["ask"]) == ("marker", True, "Ship v2 or hold?")
    assert rec["schema"] == 1 and rec["session_id"] == sid


def test_missing_tail_blocks_twice_then_heuristic(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_COCKPIT", "1")
    _r, _g, _sid, d, payload = _seed(tmp_path, NO_TAIL)
    first = _run(payload)
    second = _run({**payload, "stop_hook_active": True})
    assert _blocked(first) and _blocked(second)
    assert first["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert not (d / ti.RECORD_NAME).exists()
    third = _run({**payload, "stop_hook_active": True})
    assert not _blocked(third)
    rec = _record(d)
    assert rec["source"] == "heuristic" and rec["needs_pm"] is False
    assert not (d / ti.COUNTER_NAME).exists()


def test_signal_unset_with_role_marker_never_blocks(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_COCKPIT", raising=False)
    _r, _g, _sid, d, payload = _seed(tmp_path, NO_TAIL)
    assert not _blocked(_run(payload))
    assert _record(d)["source"] == "heuristic"
    assert not (d / ti.COUNTER_NAME).exists()


def test_signal_unset_declared_tail_still_honoured(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_COCKPIT", raising=False)
    _r, _g, _sid, d, payload = _seed(tmp_path, f"Done.\n{TAIL}")
    assert not _blocked(_run(payload))
    assert _record(d)["source"] == "marker"


def test_marker_after_one_block_resets_counter(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_COCKPIT", "1")
    _r, _g, _sid, d, payload = _seed(tmp_path, NO_TAIL)
    assert _blocked(_run(payload))
    assert ti.read_blocks(str(d)) == 1
    entry = {"type": "assistant", "message": {"content": [{"type": "text", "text": f"ok {TAIL}"}]}}
    Path(payload["transcript_path"]).write_text(json.dumps(entry) + "\n", encoding="utf-8")
    assert not _blocked(_run(payload))
    assert ti.read_blocks(str(d)) == 0
    assert _record(d)["source"] == "marker"


def test_malformed_tail_labels_without_block(tmp_path):
    _r, _g, _sid, d, payload = _seed(tmp_path, "Done.\n<turn-intent>{nope</turn-intent>")
    assert not _blocked(_run(payload))
    assert _record(d)["source"] == "marker-malformed"


def test_no_role_marker_records_heuristic_and_never_blocks(tmp_path):
    _r, _g, _sid, d, payload = _seed(tmp_path, "Found it. Your call on the rollout.", role=False)
    assert not _blocked(_run(payload))
    rec = _record(d)
    assert rec["source"] == "heuristic" and rec["needs_pm"] is True
    assert "Your call" in rec["ask"]


def test_subagent_is_exempt(tmp_path):
    _r, _g, _sid, d, payload = _seed(tmp_path, NO_TAIL)
    assert _run({**payload, "agent_id": "a1"}) == {}
    assert not (d / ti.RECORD_NAME).exists() and not (d / ti.COUNTER_NAME).exists()


def test_non_uuid_session_writes_nothing(tmp_path):
    _r, git_dir, _sid, _d, payload = _seed(tmp_path, NO_TAIL, role=False)
    payload["session_id"] = "not-a-uuid"
    assert _run(payload) == {}
    assert not (git_dir / "coordinator-sessions" / "not-a-uuid").exists()


def test_last_assistant_message_preferred_for_intent(tmp_path):
    _r, _g, _sid, d, payload = _seed(tmp_path, NO_TAIL)
    payload["last_assistant_message"] = f"Hi\n{TAIL}"
    assert not _blocked(_run(payload))
    assert _record(d)["source"] == "marker"


def test_ask_containing_your_call_is_recorded_verbatim(tmp_path):
    tail = '<turn-intent>{"needs_pm":true,"ask":"your call on X"}</turn-intent>'
    _r, _g, _sid, d, payload = _seed(tmp_path, f"Report ready.\n{tail}")
    assert not _blocked(_run(payload))
    assert _record(d)["ask"] == "your call on X"


def test_no_transcript_and_no_message_is_silent(tmp_path):
    _r, _g, _sid, d, payload = _seed(tmp_path, NO_TAIL)
    del payload["transcript_path"]
    assert _run(payload) == {}
    assert not (d / ti.RECORD_NAME).exists()


def test_flat_payload_shape_is_read(tmp_path):
    _r, _g, _sid, d, payload = _seed(tmp_path, f"Done.\n{TAIL}")
    gti._handler(dict(payload))
    assert _record(d)["source"] == "marker"


@pytest.mark.parametrize("value,expected", [("1", True), ("TRUE", True), (" true ", True), ("0", False), ("", False)])
def test_under_cockpit_values(value, expected):
    assert under_cockpit({"COORDINATOR_COCKPIT": value}) is expected
