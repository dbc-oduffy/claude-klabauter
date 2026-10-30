"""The Stop fan-in nudges once when the final assistant text drafts a research or plan sequence."""

from __future__ import annotations

import asyncio
import json
import tempfile

import pytest

from coordinator_core.hooks import stop_dispatch as sd

PLAN_TEXT = "Research plan:\n1. read the census\n2. size the work\n3. write the plan\n"


def _user(text):
    return {"type": "user", "message": {"content": text}}


def _assistant(text=None, tool=None, tool_input=None):
    blocks = []
    if tool:
        blocks.append({"type": "tool_use", "name": tool, "input": tool_input or {}})
    if text is not None:
        blocks.append({"type": "text", "text": text})
    return {"type": "assistant", "message": {"content": blocks}}


def _transcript(tmp_path, records):
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def stop(monkeypatch, tmp_path):
    """The default Stop entry with every other leg and producer silenced."""
    monkeypatch.setattr(sd, "_FOLDED_LEGS", ())
    monkeypatch.setattr(sd, "_group_em_park_spool_handler", lambda _p: None)

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(sd, "_receiver_state_sensor_handler", _noop)
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sentinels))

    def run(records, **extra):
        payload = {
            "cwd": "",
            "session_id": "sess-research-drafting",
            "transcript_path": _transcript(tmp_path, records),
            **extra,
        }
        return asyncio.run(sd._handler({"payload": payload}))

    return run


def _nudged(result):
    return "scoping research by hand" in json.dumps(result)


def test_drafted_plan_sequence_emits_nudge_with_latest_pm_ask(stop):
    result = stop([_user("research the census gap"), _assistant(PLAN_TEXT)])
    assert _nudged(result)
    assert "research the census gap" in json.dumps(result)
    assert result.get("decision") != "block"
    assert "permissionDecision" not in json.dumps(result)


def test_nudge_fires_once_per_turn(stop):
    records = [_user("go"), _assistant(PLAN_TEXT)]
    assert _nudged(stop(records))
    assert not _nudged(stop(records))


def test_prose_without_step_run_is_silent(stop):
    assert not _nudged(stop([_user("go"), _assistant("The research plan is fine, nothing to list.")]))


def test_step_run_without_research_or_plan_is_silent(stop):
    assert not _nudged(stop([_user("go"), _assistant("Done:\n1. a\n2. b\n3. c\n")]))


def test_engine_ask_in_flight_is_silent(stop):
    records = [
        _user("go"),
        _assistant(tool="Bash", tool_input={"command": "engine-ask --ask x"}),
        _assistant(PLAN_TEXT),
    ]
    assert not _nudged(stop(records))


def test_prior_turn_fire_does_not_silence_new_turn(stop):
    records = [
        _user("one"),
        _assistant(tool="Skill", tool_input={"skill": "coordinator:research"}),
        _user("two"),
        _assistant(PLAN_TEXT),
    ]
    assert _nudged(stop(records))


def test_chained_stop_and_subagent_are_silent(stop):
    records = [_user("go"), _assistant(PLAN_TEXT)]
    assert not _nudged(stop(records, stop_hook_active=True))
    assert not _nudged(stop(records, agent_id="sub-1"))


def test_kill_switch_is_silent(stop):
    records = [_user("go"), _assistant(PLAN_TEXT)]
    assert not _nudged(stop(records, env={"COORDINATOR_HOOK_NUDGE_EM_RESEARCH_SCOPING_DISABLED": "1"}))


def test_unreadable_transcript_is_silent(stop):
    assert not _nudged(stop([_user("go")], transcript_path="/no/such/transcript.jsonl"))
