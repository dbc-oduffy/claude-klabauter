"""Tests for coordinator_core.hooks.flag_em_poll_in_flight (plan
2026-09-27-four-turn-em-loop.md C17). Warm, zero-spawn: no subprocess, one bounded file read.
"""
from __future__ import annotations

import glob
import json
import os
import tempfile
import uuid
from pathlib import Path

import pytest

from coordinator_core.hooks import flag_em_poll_in_flight as mod


def _cleanup(session_id: str) -> None:
    tmpdir = tempfile.gettempdir()
    for pattern in (
        f"em-poll-count-{session_id}.json",
        f".em-poll-count-*.tmp",
        f"workflow-run-{session_id}-*.json",
    ):
        for path in glob.glob(os.path.join(tmpdir, pattern)):
            try:
                os.unlink(path)
            except OSError:
                pass


@pytest.fixture()
def session_id():
    sid = f"test-{uuid.uuid4().hex[:8]}"
    yield sid
    _cleanup(sid)


@pytest.fixture()
def transcript(tmp_path, session_id):
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text("", encoding="utf-8")
    return path


def _write_tail(path: Path, lines: list) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _tu(name: str) -> str:
    """A real-shape assistant envelope line carrying one tool_use block."""
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "x"},
            {"type": "tool_use", "id": "toolu_1", "name": name, "input": {}},
        ]},
    })


def _tool_result(text: str = "ok") -> str:
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": text}]}})


_BOUNDARY_LINE = json.dumps(
    {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted"}
)


def _payload(session_id, transcript_path, tool_name, agent_id=None):
    p = {"session_id": session_id, "transcript_path": str(transcript_path), "tool_name": tool_name}
    if agent_id:
        p["agent_id"] = agent_id
    return p


def _seed_run_record(session_id: str, run_id: str) -> None:
    tmpdir = tempfile.gettempdir()
    path = os.path.join(tmpdir, f"workflow-run-{session_id}-task1.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"run_id": run_id, "session_id": session_id}, fh)


def test_first_poll_is_silent(session_id, transcript):
    _write_tail(transcript, [_tu("Bash")])
    result = mod._handler(_payload(session_id, transcript, "ReadNotifications"))
    assert result == {}


def test_second_consecutive_poll_with_live_run_flags(session_id, transcript):
    _seed_run_record(session_id, "run-abc")
    _write_tail(transcript, [_tu("ReadNotifications")])
    mod._handler(_payload(session_id, transcript, "ReadNotifications"))
    result = mod._handler(_payload(session_id, transcript, "ReadNotifications"))
    assert result != {}
    assert "poll" in result["hookSpecificOutput"]["additionalContext"].lower()


def test_tail_with_intervening_bash_resets(session_id, transcript):
    _seed_run_record(session_id, "run-abc")
    _write_tail(transcript, [_tu("ReadNotifications")])
    mod._handler(_payload(session_id, transcript, "ReadNotifications"))
    _write_tail(transcript, [_tu("Bash")])
    result = mod._handler(_payload(session_id, transcript, "ReadNotifications"))
    assert result == {}


def test_run_completion_in_tail_is_silent(session_id, transcript):
    _seed_run_record(session_id, "run-abc")
    _write_tail(transcript, [_tu("ReadNotifications")])
    mod._handler(_payload(session_id, transcript, "ReadNotifications"))
    _write_tail(
        transcript,
        [_tu("ReadNotifications"), _tool_result("run-abc completed and landed")],
    )
    result = mod._handler(_payload(session_id, transcript, "ReadNotifications"))
    assert result == {}


def test_subagent_payload_is_silent(session_id, transcript):
    _seed_run_record(session_id, "run-abc")
    _write_tail(transcript, [_tu("ReadNotifications")])
    mod._handler(_payload(session_id, transcript, "ReadNotifications"))
    result = mod._handler(_payload(session_id, transcript, "ReadNotifications", agent_id="agent-1"))
    assert result == {}


def test_compact_boundary_is_silent(session_id, transcript):
    _seed_run_record(session_id, "run-abc")
    _write_tail(transcript, [_tu("ReadNotifications")])
    mod._handler(_payload(session_id, transcript, "ReadNotifications"))
    _write_tail(
        transcript,
        [_BOUNDARY_LINE, _tu("ReadNotifications")],
    )
    result = mod._handler(_payload(session_id, transcript, "ReadNotifications"))
    assert result == {}


def test_old_compaction_in_the_tail_no_longer_exempts(session_id, transcript):
    """A compaction boundary that is NOT the most recent event (an intervening poll already
    consumed it) must not keep exempting every later poll -- only the first read right after
    the boundary is exempt (P2 fix: `.search()` over the whole tail was over-broad)."""
    _seed_run_record(session_id, "run-abc")
    _write_tail(
        transcript,
        [
            _BOUNDARY_LINE,
            _tu("ReadNotifications"),
        ],
    )
    # First poll right after the boundary: exempt (boundary is the most recent event).
    result = mod._handler(_payload(session_id, transcript, "ReadNotifications"))
    assert result == {}
    # Second consecutive poll: the boundary is now stale (an intervening poll's tool_use
    # record sits after it in the tail) -- must flag.
    _write_tail(
        transcript,
        [
            _BOUNDARY_LINE,
            _tu("ReadNotifications"),
            _tu("ReadNotifications"),
        ],
    )
    result = mod._handler(_payload(session_id, transcript, "ReadNotifications"))
    assert result != {}
    assert "poll" in result["hookSpecificOutput"]["additionalContext"].lower()


def test_missing_transcript_fails_open_silently(session_id):
    result = mod._handler(
        _payload(session_id, "/no/such/transcript-file.jsonl", "ReadNotifications")
    )
    assert result == {}


def test_no_in_flight_run_is_silent(session_id, transcript):
    _write_tail(transcript, [_tu("ReadNotifications")])
    mod._handler(_payload(session_id, transcript, "ReadNotifications"))
    result = mod._handler(_payload(session_id, transcript, "ReadNotifications"))
    assert result == {}


def test_flat_shape_is_not_the_production_shape(session_id, transcript):
    """A bare `{"type":"tool_use",...}` line is not what Claude Code writes; the hook must
    read only the nested assistant envelope."""
    _seed_run_record(session_id, "run-abc")
    _write_tail(transcript, ['{"type":"tool_use","name":"ReadNotifications"}'] * 2)
    assert mod._handler(_payload(session_id, transcript, "ReadNotifications")) == {}
    assert mod._events(transcript.read_text()) == []


def test_malformed_and_truncated_lines_are_tolerated(session_id, transcript):
    _seed_run_record(session_id, "run-abc")
    _write_tail(transcript, ['ion":"x"}]}}', "not json", _tool_result(), _tu("ReadNotifications")])
    result = mod._handler(_payload(session_id, transcript, "ReadNotifications"))
    assert result != {}


def test_real_shape_fixture_flags_after_boundary_grace(session_id):
    fixture = Path(mod.__file__).parent / "fixtures" / "em_poll_transcript_tail.jsonl"
    events = mod._events(fixture.read_text(encoding="utf-8"))
    assert events == ["Bash", "Workflow", mod._BOUNDARY, "ReadNotifications", "ReadNotifications"]
    _seed_run_record(session_id, "run-redacted-1")
    result = mod._handler(_payload(session_id, fixture, "ReadNotifications"))
    assert result != {}
