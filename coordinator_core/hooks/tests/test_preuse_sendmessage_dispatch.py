from __future__ import annotations

import json

from coordinator_core.hooks import preuse_sendmessage_dispatch as m


def _run(tmp_path, members=("a1", "b2"), roster=True, name="wf_1"):
    run = tmp_path / "sess" / "subagents" / "workflows" / name
    run.mkdir(parents=True, exist_ok=True)
    for a in members:
        (run / f"agent-{a}.jsonl").write_text("")
    if roster:
        (run / "roster.json").write_text(
            json.dumps({"members": [{"role": f"r{i}", "agent_id": a} for i, a in enumerate(members)]})
        )
    return run


def _p(run, agent):
    return {
        "tool_name": "SendMessage",
        "agent_id": agent,
        "agent_type": "workflow-subagent",
        "transcript_path": str(run.parents[2]) + ".jsonl",
        "tool_input": {"to": "x", "message": "hi"},
    }


def _ctx(env):
    return (env.get("hookSpecificOutput") or {}).get("additionalContext") or ""


def _no_deny(env):
    assert "permissionDecision" not in (env.get("hookSpecificOutput") or {})


def test_first_send_advises_second_silent_other_agent_advised(tmp_path):
    run = _run(tmp_path)
    first = m._handler(_p(run, "a1"))
    assert "Summarise" in _ctx(first)
    _no_deny(first)
    assert m._handler(_p(run, "a1")) == {}
    assert "Summarise" in _ctx(m._handler(_p(run, "b2")))


def test_no_roster_noop(tmp_path):
    run = _run(tmp_path, roster=False)
    assert m._handler(_p(run, "a1")) == {}


def test_non_member_noop(tmp_path):
    run = _run(tmp_path)
    assert m._handler(_p(run, "zz")) == {}


def test_malformed_roster_and_stdin_allow_silently(tmp_path):
    run = _run(tmp_path)
    (run / "roster.json").write_text("{not json")
    assert m._handler(_p(run, "a1")) == {}
    for bad in ({}, {"tool_name": "SendMessage"}, {"tool_name": "SendMessage", "agent_id": 3,
                 "transcript_path": 4}, {"tool_name": "Bash"}, None, "x"):
        env = m._handler(bad)
        assert env == {}


def test_unwritable_state_fails_open(tmp_path):
    run = _run(tmp_path)
    (run / "advised").write_text("file blocks dir")
    assert m._handler(_p(run, "a1")) == {}


def test_zero_match_noop(tmp_path):
    run = _run(tmp_path)
    (run / "agent-a1.jsonl").unlink()
    assert m._handler(_p(run, "a1")) == {}


def test_two_match_noop(tmp_path):
    run = _run(tmp_path)
    _run(tmp_path, name="wf_2")
    assert m._handler(_p(run, "a1")) == {}
