"""Reply-resume identity: a same-type second write for a recorded agentId is the
same agent, as is the harness's workflow-subagent -> general-purpose resume
re-report; any other different real type stays AMBIGUOUS and confined.
"""

from __future__ import annotations

import coordinator_core.hooks.track_dispatched_agents as tda
from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist as guard

TYPE = "coordinator:executor"
ROSTER = frozenset({TYPE, "coordinator:code-reviewer"})


def test_same_type_resume_leaves_row_untouched():
    row = ["a1", "opus", TYPE, "1700000000"]
    assert tda._resolve_row_collision(row, "opus", TYPE) is None
    assert tda._resolve_row_collision(row, "unknown", TYPE) is None


def test_resume_with_placeholder_type_never_downgrades_or_collides():
    row = ["a1", "opus", TYPE, "1700000000"]
    assert tda._resolve_row_collision(row, "unknown", tda.PLACEHOLDER_TYPE) is None


def test_cross_type_reuse_is_ambiguous():
    cols = tda._resolve_row_collision(["a1", "opus", TYPE, "1"], "opus", "coordinator:code-reviewer")
    assert cols is not None and cols[2] == tda.AMBIGUOUS_TYPE


def test_first_dispatch_unchanged_by_resume_seam():
    assert tda._is_reply_resume(TYPE, "Coordinator:Executor")
    assert not tda._is_reply_resume(TYPE, "coordinator:code-reviewer")


def _wire(monkeypatch, row_type):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake")
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, sid: "deadbeef0123")
    monkeypatch.setattr(guard, "_read_backpointer_subagent_type", lambda *a, **k: row_type)
    monkeypatch.setattr(
        guard, "is_confined_by_roster_absence", lambda t: bool(t) and t not in ROSTER
    )


def _bash(cmd):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": "s1",
        "cwd": None,
        "agent_id": "deadbeef0123",
        "agent_type": TYPE,
    }


def test_resumed_same_type_row_is_not_confined(monkeypatch):
    _wire(monkeypatch, TYPE)
    assert guard.check(_bash("git status")) is None
    assert guard.check(_bash("python3 -c 1")) is None


def test_ambiguous_row_still_confines(monkeypatch):
    _wire(monkeypatch, tda.AMBIGUOUS_TYPE)
    res = guard.check(_bash("python3 -c 1"))
    assert res is not None
    assert res["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_workflow_agent_resume_reported_as_general_purpose_is_the_same_agent():
    # Row shape and incoming type as captured by DoE spike S2 (2026-09-30).
    row = ["a1", "unknown", "workflow-subagent", "1700000000"]
    assert tda._resolve_row_collision(row, "unknown", "general-purpose") is None


def test_resume_pair_is_one_directional_and_exact():
    assert not tda._is_reply_resume("general-purpose", "workflow-subagent")
    cols = tda._resolve_row_collision(
        ["a1", "unknown", "workflow-subagent", "1"], "opus", "coordinator:executor"
    )
    assert cols is not None and cols[2] == tda.AMBIGUOUS_TYPE
