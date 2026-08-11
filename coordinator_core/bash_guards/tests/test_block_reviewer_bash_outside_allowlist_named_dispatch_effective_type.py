"""Regression suite for Divergence 16 (2026-08-11) of
``block_reviewer_bash_outside_allowlist`` -- ``effective_type`` resolution
for a NAMED (Agent-teams teammate) dispatch.

Root cause pinned at
``docs/problems/2026-08-11-a-dispatched-coordinator-executor-is-den.md``: for
a named dispatch, ``payload["agent_type"]`` carries the teammate's own
``name`` string, not the dispatched agent's real ``coordinator:*`` type (the
real type is only recoverable via the back-pointer-resolved
``subagent_type``, per the module's own Design-section note). Before this
fix, the raw name string -- correctly confined by Divergence 15's
roster-absence leg 3, since it genuinely is not a known type -- WON priority
over the correctly-resolved ``subagent_type`` in ``effective_type``
selection, so a named ``coordinator:executor`` was ruled under the
conservative reviewer-shaped default ruleset instead of its own.

Pure Python -- no real ``resolve_roster()`` call; ``is_confined_by_roster_
absence`` is monkeypatched directly per case, matching the sibling roster-
absence suite's own convention (see that file's docstring) -- this file does
NOT use the main oracle suite's autouse fixture that stubs the roster leg to
always-False, since these tests need the leg to distinguish a known type
from a garbage teammate-name string.

Spec backlink: coordinator_core/bash_guards/block_reviewer_bash_outside_allowlist.py § Divergence 16
docs/problems/2026-08-11-a-dispatched-coordinator-executor-is-den.md
"""

from __future__ import annotations

from coordinator_core.bash_guards import (
    block_reviewer_bash_outside_allowlist as guard,
)

_EXECUTOR_TYPE = "coordinator:executor"
_REVIEWER_TYPE = "coordinator:code-reviewer"
_ENUMERATED_TYPES = frozenset({_EXECUTOR_TYPE, _REVIEWER_TYPE, "coordinator:enricher"})
_TEAMMATE_NAME = "archive-guard"
_INVENTED_TYPE = "hookprobe-named"


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
    """Simulate a NAMED dispatch: the back-pointer resolves the real type
    (``subagent_type``); the payload's own ``agent_type`` will carry the
    teammate name instead (set by the caller via ``_payload``)."""
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id: subagent_type,
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


# ---------------------------------------------------------------------------
# The confirmed defect: named coordinator:executor gets its OWN ruleset.
# ---------------------------------------------------------------------------


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


def test_named_executor_curl_still_denied(monkeypatch):
    """The fix must not unconfine the named executor -- only give it its
    own ruleset. curl is outside every ruleset's allowlist."""
    _wire_named_dispatch(monkeypatch, subagent_type=_EXECUTOR_TYPE)
    payload = _payload("curl https://evil.example/x", agent_type=_TEAMMATE_NAME)
    _assert_denied(guard.check(payload))


# ---------------------------------------------------------------------------
# Unnamed dispatch -- unaffected (no regression).
# ---------------------------------------------------------------------------


def test_unnamed_executor_pytest_still_allowed(monkeypatch):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard, "_read_backpointer_subagent_type", lambda git_root, agent_id: ""
    )
    monkeypatch.setattr(guard, "is_confined_by_roster_absence", lambda t: False)
    payload = _payload("python3 -m pytest -q", agent_type=_EXECUTOR_TYPE)
    _assert_allowed(guard.check(payload))


# ---------------------------------------------------------------------------
# coordinator:code-reviewer parity -- byte-identical, unaffected by this fix.
# ---------------------------------------------------------------------------


def test_reviewer_unnamed_behaviour_unchanged(monkeypatch):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard, "_read_backpointer_subagent_type", lambda git_root, agent_id: ""
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
    """coordinator:code-reviewer shares the pytest module allowance
    (Amendment 2) -- must resolve identically whether named or unnamed."""
    _wire_named_dispatch(monkeypatch, subagent_type=_REVIEWER_TYPE)
    payload = _payload("python3 -m pytest -q", agent_type=_TEAMMATE_NAME)
    _assert_allowed(guard.check(payload))


# ---------------------------------------------------------------------------
# Leg 3 (unenumerated roster absence) stays fail-closed -- for BOTH the
# named and unnamed shape -- a made-up type is still confined and gets the
# conservative default ruleset, never a real type's override.
# ---------------------------------------------------------------------------


def test_unnamed_invented_type_still_confined_and_denied(monkeypatch):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard, "_read_backpointer_subagent_type", lambda git_root, agent_id: ""
    )
    monkeypatch.setattr(
        guard, "is_confined_by_roster_absence", lambda t: bool(t) and t not in _ENUMERATED_TYPES
    )
    payload = _payload("python3 -m pytest -q", agent_type=_INVENTED_TYPE)
    _assert_denied(guard.check(payload))


def test_named_invented_type_still_confined_and_denied(monkeypatch):
    """Both legs unresolved to a known identity (teammate name AND the
    dispatched type itself are unenumerated) -- must stay confined under the
    conservative default, not silently allow."""
    _wire_named_dispatch(monkeypatch, subagent_type=_INVENTED_TYPE)
    payload = _payload("python3 -m pytest -q", agent_type=_TEAMMATE_NAME)
    _assert_denied(guard.check(payload))


# ---------------------------------------------------------------------------
# Divergence 17 (2026-08-11) -- close the type-smuggling residual: a
# caller-chosen name that happens to be a literal known type string must
# never outrank a back-pointer-derived subagent_type when the two disagree.
# ---------------------------------------------------------------------------


def test_disagreeing_known_agent_type_loses_to_subagent_type(monkeypatch):
    """A coordinator:code-reviewer dispatched with name="coordinator:executor"
    must resolve to the reviewer's own ruleset, not the wider executor one --
    the hole this divergence closes."""
    _wire_named_dispatch(monkeypatch, subagent_type=_REVIEWER_TYPE)
    resolved = guard._resolve_effective_type(_EXECUTOR_TYPE, _REVIEWER_TYPE, policy=None)
    assert resolved == _REVIEWER_TYPE


def test_disagreeing_known_agent_type_denied_executor_only_surface(monkeypatch):
    """Concretely: a reviewer named "coordinator:executor" must NOT get the
    executor-only interpreter_allow_scripts surface (bare python3 <script>)."""
    _wire_named_dispatch(monkeypatch, subagent_type=_REVIEWER_TYPE)
    payload = _payload("python3 /tmp/some_script.py", agent_type=_EXECUTOR_TYPE)
    _assert_denied(guard.check(payload))


def test_bullet1_named_free_text_agent_type_resolves_to_real_subagent_type(monkeypatch):
    """Divergence 16's original fix must not regress: agent_type free text,
    subagent_type the real type -> real type wins."""
    _wire_named_dispatch(monkeypatch, subagent_type=_EXECUTOR_TYPE)
    resolved = guard._resolve_effective_type(_TEAMMATE_NAME, _EXECUTOR_TYPE, policy=None)
    assert resolved == _EXECUTOR_TYPE


def test_bullet2_disagreeing_known_types_resolves_to_subagent_type(monkeypatch):
    """The hole being closed: agent_type is itself a known type string that
    disagrees with a known subagent_type -> subagent_type (the back-pointer)
    wins, not the caller-chosen name."""
    _wire_named_dispatch(monkeypatch, subagent_type=_REVIEWER_TYPE)
    resolved = guard._resolve_effective_type(_EXECUTOR_TYPE, _REVIEWER_TYPE, policy=None)
    assert resolved == _REVIEWER_TYPE


def test_bullet3_unnamed_dispatch_resolves_to_agent_type(monkeypatch):
    """Unnamed dispatch: agent_type is the real type, subagent_type is
    empty/absent -> must still resolve to agent_type, exactly as today."""
    monkeypatch.setattr(guard, "is_confined_by_roster_absence", lambda t: bool(t) and t not in _ENUMERATED_TYPES)
    resolved = guard._resolve_effective_type(_EXECUTOR_TYPE, "", policy=None)
    assert resolved == _EXECUTOR_TYPE


def test_bullet4_both_legs_unknown_degrades_to_original_selection(monkeypatch):
    """Both legs unknown -> unchanged, fail-closed degrade to the original
    _is_confined_type-based selection (agent_type first)."""
    monkeypatch.setattr(guard, "is_confined_by_roster_absence", lambda t: bool(t) and t not in _ENUMERATED_TYPES)
    resolved = guard._resolve_effective_type(_TEAMMATE_NAME, _INVENTED_TYPE, policy=None)
    assert resolved == _TEAMMATE_NAME
