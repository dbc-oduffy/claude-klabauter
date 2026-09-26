
from __future__ import annotations

from coordinator_core.bash_guards import (
    block_reviewer_bash_outside_allowlist as guard,
)

_EXECUTOR_TYPE = "coordinator:executor"
_REVIEWER_TYPE = "coordinator:code-reviewer"
_ENUMERATED_TYPES = frozenset({_EXECUTOR_TYPE, _REVIEWER_TYPE, "coordinator:enricher"})
_TEAMMATE_NAME = "archive-guard"
_INVENTED_TYPE = "hookprobe-named"
_GIT_COMMIT_AGENT_TYPE = "coordinator:git-commit-agent"
_ENUMERATED_TYPES_WITH_COMMIT_AGENT = _ENUMERATED_TYPES | {_GIT_COMMIT_AGENT_TYPE}


def _payload(command, agent_id="deadbeef0123", agent_type=None, session_id="sess1"):
    p: dict = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": session_id,
        "cwd": None,
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    if agent_type is not None:
        p["agent_type"] = agent_type
    return p


def _wire_named_dispatch(monkeypatch, subagent_type):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id, **kw: subagent_type,
    )
    monkeypatch.setattr(
        guard,
        "is_confined_by_roster_absence",
        lambda t: bool(t) and t not in _ENUMERATED_TYPES,
    )


def _assert_denied(result):
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def _assert_allowed(result):
    assert result is None


def test_named_executor_resolves_effective_type_to_executor(monkeypatch):
    _wire_named_dispatch(monkeypatch, subagent_type=_EXECUTOR_TYPE)
    resolved = guard._resolve_effective_type(_TEAMMATE_NAME, _EXECUTOR_TYPE, policy=None)
    assert resolved == _EXECUTOR_TYPE


def test_named_executor_pytest_allowed(monkeypatch):
    _wire_named_dispatch(monkeypatch, subagent_type=_EXECUTOR_TYPE)
    payload = _payload("python3 -m pytest -q", agent_type=_TEAMMATE_NAME)
    _assert_allowed(guard.check(payload))


def test_named_executor_pytest_with_stderr_redirect_allowed(monkeypatch):
    _wire_named_dispatch(monkeypatch, subagent_type=_EXECUTOR_TYPE)
    payload = _payload("python3 -m pytest -q 2>&1", agent_type=_TEAMMATE_NAME)
    _assert_allowed(guard.check(payload))


def test_named_executor_curl_allowed_because_executor_is_not_confined(monkeypatch):
    """(Divergence 18, 2026-08-14 correction) This test previously asserted
    curl was denied for a named coordinator:executor -- that assertion only
    held because the pre-Divergence-18 unconditional OR manufactured
    confinement via the teammate-name leg (coordinator:executor itself is
    NOT a member of _CONFINED_FINDINGS_AGENTS -- DR-125 deliberately removed
    it, see _helpers.py's own comment -- and carries no bash_policy row in
    this dev environment's real, unmocked load_policy() either). With the
    fix, a KNOWN, non-confined subagent_type governs the verdict outright,
    so a named coordinator:executor is not confined at all and curl is
    allowed -- exactly the same as an unnamed coordinator:executor already
    was before this divergence (named/unnamed parity is the point of the
    fix). The prior denial encoded the defect, not a real safeguard."""
    _wire_named_dispatch(monkeypatch, subagent_type=_EXECUTOR_TYPE)
    payload = _payload("curl https://evil.example/x", agent_type=_TEAMMATE_NAME)
    _assert_allowed(guard.check(payload))


def test_unnamed_executor_pytest_still_allowed(monkeypatch):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard, "_read_backpointer_subagent_type", lambda git_root, agent_id, **kw: ""
    )
    monkeypatch.setattr(guard, "is_confined_by_roster_absence", lambda t: False)
    payload = _payload("python3 -m pytest -q", agent_type=_EXECUTOR_TYPE)
    _assert_allowed(guard.check(payload))


def test_reviewer_unnamed_behaviour_unchanged(monkeypatch):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard, "_read_backpointer_subagent_type", lambda git_root, agent_id, **kw: ""
    )
    monkeypatch.setattr(guard, "is_confined_by_roster_absence", lambda t: False)
    payload = _payload('git commit -m "x"', agent_type=_REVIEWER_TYPE)
    reason = _assert_denied(guard.check(payload))
    assert "coordinator-doc-new" in reason


def test_named_reviewer_resolves_effective_type_to_reviewer_and_denies_commit(monkeypatch):
    _wire_named_dispatch(monkeypatch, subagent_type=_REVIEWER_TYPE)
    payload = _payload('git commit -m "x"', agent_type=_TEAMMATE_NAME)
    reason = _assert_denied(guard.check(payload))
    assert "coordinator-doc-new" in reason


def test_named_reviewer_pytest_still_allowed_via_amendment_2(monkeypatch):
    _wire_named_dispatch(monkeypatch, subagent_type=_REVIEWER_TYPE)
    payload = _payload("python3 -m pytest -q", agent_type=_TEAMMATE_NAME)
    _assert_allowed(guard.check(payload))


def test_unnamed_invented_type_still_confined_and_denied(monkeypatch):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard, "_read_backpointer_subagent_type", lambda git_root, agent_id, **kw: ""
    )
    monkeypatch.setattr(
        guard, "is_confined_by_roster_absence", lambda t: bool(t) and t not in _ENUMERATED_TYPES
    )
    payload = _payload("python3 -m pytest -q", agent_type=_INVENTED_TYPE)
    _assert_denied(guard.check(payload))


def test_named_invented_type_still_confined_and_denied(monkeypatch):
    _wire_named_dispatch(monkeypatch, subagent_type=_INVENTED_TYPE)
    payload = _payload("python3 -m pytest -q", agent_type=_TEAMMATE_NAME)
    _assert_denied(guard.check(payload))


def test_disagreeing_known_agent_type_loses_to_subagent_type(monkeypatch):
    _wire_named_dispatch(monkeypatch, subagent_type=_REVIEWER_TYPE)
    resolved = guard._resolve_effective_type(_EXECUTOR_TYPE, _REVIEWER_TYPE, policy=None)
    assert resolved == _REVIEWER_TYPE


def test_disagreeing_known_agent_type_denied_executor_only_surface(monkeypatch):
    _wire_named_dispatch(monkeypatch, subagent_type=_REVIEWER_TYPE)
    payload = _payload("python3 /tmp/some_script.py", agent_type=_EXECUTOR_TYPE)
    _assert_denied(guard.check(payload))


def test_bullet1_named_free_text_agent_type_resolves_to_real_subagent_type(monkeypatch):
    _wire_named_dispatch(monkeypatch, subagent_type=_EXECUTOR_TYPE)
    resolved = guard._resolve_effective_type(_TEAMMATE_NAME, _EXECUTOR_TYPE, policy=None)
    assert resolved == _EXECUTOR_TYPE


def test_bullet2_disagreeing_known_types_resolves_to_subagent_type(monkeypatch):
    _wire_named_dispatch(monkeypatch, subagent_type=_REVIEWER_TYPE)
    resolved = guard._resolve_effective_type(_EXECUTOR_TYPE, _REVIEWER_TYPE, policy=None)
    assert resolved == _REVIEWER_TYPE


def test_bullet3_unnamed_dispatch_resolves_to_agent_type(monkeypatch):
    monkeypatch.setattr(guard, "is_confined_by_roster_absence", lambda t: bool(t) and t not in _ENUMERATED_TYPES)
    resolved = guard._resolve_effective_type(_EXECUTOR_TYPE, "", policy=None)
    assert resolved == _EXECUTOR_TYPE


def test_bullet4_both_legs_unknown_degrades_to_original_selection(monkeypatch):
    monkeypatch.setattr(guard, "is_confined_by_roster_absence", lambda t: bool(t) and t not in _ENUMERATED_TYPES)
    resolved = guard._resolve_effective_type(_TEAMMATE_NAME, _INVENTED_TYPE, policy=None)
    assert resolved == _TEAMMATE_NAME


def _wire_named_dispatch_enumerated(monkeypatch, subagent_type):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id, **kw: subagent_type,
    )
    monkeypatch.setattr(
        guard,
        "is_confined_by_roster_absence",
        lambda t: bool(t) and t not in _ENUMERATED_TYPES_WITH_COMMIT_AGENT,
    )


def test_named_git_commit_agent_scoped_git_commit_allowed(monkeypatch):
    _wire_named_dispatch_enumerated(monkeypatch, subagent_type=_GIT_COMMIT_AGENT_TYPE)
    payload = _payload(
        "scoped-git-commit --pathspec foo.py -m x", agent_type=_TEAMMATE_NAME
    )
    _assert_allowed(guard.check(payload))


def test_named_unnamed_parity_for_non_confined_type(monkeypatch):
    command = "scoped-git-commit --pathspec foo.py -m x"

    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard, "_read_backpointer_subagent_type", lambda git_root, agent_id, **kw: ""
    )
    monkeypatch.setattr(
        guard,
        "is_confined_by_roster_absence",
        lambda t: bool(t) and t not in _ENUMERATED_TYPES_WITH_COMMIT_AGENT,
    )
    unnamed_payload = _payload(command, agent_type=_GIT_COMMIT_AGENT_TYPE)
    unnamed_result = guard.check(unnamed_payload)

    _wire_named_dispatch_enumerated(monkeypatch, subagent_type=_GIT_COMMIT_AGENT_TYPE)
    named_payload = _payload(command, agent_type=_TEAMMATE_NAME)
    named_result = guard.check(named_payload)

    assert unnamed_result is None
    assert named_result is None
    assert unnamed_result == named_result


def test_named_dispatch_unresolvable_subagent_type_still_fail_closed(monkeypatch):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard, "_read_backpointer_subagent_type", lambda git_root, agent_id, **kw: ""
    )
    monkeypatch.setattr(
        guard,
        "is_confined_by_roster_absence",
        lambda t: bool(t) and t not in _ENUMERATED_TYPES_WITH_COMMIT_AGENT,
    )
    payload = _payload(
        "scoped-git-commit --pathspec foo.py -m x", agent_type=_TEAMMATE_NAME
    )
    _assert_denied(guard.check(payload))


def test_named_confined_subagent_type_still_denied(monkeypatch):
    _wire_named_dispatch(monkeypatch, subagent_type=_REVIEWER_TYPE)
    payload = _payload(
        "scoped-git-commit --pathspec foo.py -m x", agent_type=_TEAMMATE_NAME
    )
    _assert_denied(guard.check(payload))


def test_backpointer_expected_session_id_forwarded_from_payload(monkeypatch):
    seen = {}
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )

    def _capturing(git_root, agent_id, **kw):
        seen.update(kw)
        return _EXECUTOR_TYPE

    monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _capturing)
    monkeypatch.setattr(
        guard, "is_confined_by_roster_absence", lambda t: bool(t) and t not in _ENUMERATED_TYPES
    )
    payload = _payload("python3 -m pytest -q", agent_type=_TEAMMATE_NAME, session_id="sess-real")
    _assert_allowed(guard.check(payload))
    assert seen.get("expected_em_session_id") == "sess-real"


def test_known_confined_agent_type_not_laundered_by_nonconfined_subagent_type(monkeypatch):
    """Staff-eng review (2026-08-14, finding 0/major): a KNOWN, non-confined
    subagent_type must never clear confinement a KNOWN, genuinely-confined
    agent_type (coordinator:code-reviewer) imposes -- a stale or
    attacker-written dispatched-agents.txt row resolving to
    coordinator:enricher must not de-confine a code-reviewer. This is the
    missing quadrant: agent_type is itself KNOWN-and-CONFINED (not a
    teammate name), paired with a KNOWN-and-non-confined subagent_type.
    This test FAILS on the pre-correction diff."""
    _wire_named_dispatch(monkeypatch, subagent_type="coordinator:enricher")
    payload = _payload(
        "curl https://evil.example/x", agent_type=_REVIEWER_TYPE
    )
    _assert_denied(guard.check(payload))
