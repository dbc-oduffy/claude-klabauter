from __future__ import annotations

import pytest

from coordinator_core.hooks import block_group_em_agent_cap_grant as m

SID = "sess-group-em"


def _p(message, session_id=SID):
    return {
        "tool_name": "SendMessage",
        "session_id": session_id,
        "cwd": "/nonexistent-repo",
        "tool_input": {"to": "peer", "message": message},
    }


def _denies(env):
    return (env.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"


@pytest.fixture
def group_em(monkeypatch):
    monkeypatch.setattr(m, "_holds_standing", lambda sid, cwd: sid == SID)


@pytest.mark.parametrize(
    "message",
    [
        "GO for example-stats-repo: max 3 executors concurrent; scoped vitest.",
        "GO. Terms: at most 4 concurrent rows",
        "GO: <=2 agents in parallel",
        "slot granted, 5 agents max",
        "GO to fire, OK-3",
        "terms: limited to two executors",
        "Heads up\nGO with up to 6 waves, no builds",
    ],
)
def test_agent_cap_in_grant_denies(group_em, message):
    out = m._handler(_p(message))
    assert _denies(out)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "maxWorkers=2" in reason and "one tsc" in reason


@pytest.mark.parametrize(
    "message",
    [
        "GO: vitest --maxWorkers=2 per row, one tsc, no build",
        "GO with scoped vitest only, -n0 serial",
        "terms: one tsc at a time, no pnpm -r",
        "max 3 executors concurrent",
        "OK-3 noted",
        "six executors on disjoint files is fine, GO",
        "GO\nmax 3 executors",
        "",
    ],
)
def test_command_caps_and_ungranted_caps_pass(group_em, message):
    assert not _denies(m._handler(_p(message)))


def test_non_group_em_session_passes(group_em):
    assert not _denies(m._handler(_p("GO: max 3 executors concurrent", session_id="someone-else")))


def test_other_tool_and_malformed_payloads_pass(group_em):
    assert not _denies(m._handler({"tool_name": "Read", "tool_input": {"message": "GO max 3 executors"}}))
    assert not _denies(m._handler({"tool_name": "SendMessage", "tool_input": {"message": {"x": 1}}}))
    assert not _denies(m._handler({"tool_name": "SendMessage", "session_id": SID}))
    assert not _denies(m._handler(_p("GO: max 3 executors") | {"session_id": ""}))


def test_standing_error_fails_open(monkeypatch):
    def boom(sid, cwd):
        raise RuntimeError("x")

    monkeypatch.setattr(m, "_holds_standing", boom)
    assert not _denies(m._handler(_p("GO: max 3 executors")))


def test_deny_message_within_register_cap():
    from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES

    assert len(m._DENY_PROSE.encode()) <= MESSAGE_PROSE_CAP_BYTES
