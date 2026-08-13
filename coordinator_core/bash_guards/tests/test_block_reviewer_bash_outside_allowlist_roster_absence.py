"""AC5 regression suite (docs/plans/2026-08-10-deny-unenumerated-agent-types-
at-dispatch.md, C2): an unenumerated ``subagent_type``/``agent_type`` must be
CONFINED by ``block_reviewer_bash_outside_allowlist``, not exempt.

Before C2, a type absent from both ``_helpers._CONFINED_FINDINGS_AGENTS``
and any ``bash_policy:`` key fell through ``_is_confined_type`` to "not
confined" -- unrestricted Bash. An invented ``subagent_type`` therefore had a
WIDER Bash surface than ``coordinator:code-reviewer``. C2 adds a third,
roster-driven leg (``_helpers.is_confined_by_roster_absence``, itself a thin
consumer of C1's ``resolve_roster()``): a type absent from that roster is
now confined, while a type ON the roster but simply not chosen for
confinement (``coordinator:enricher`` and siblings) stays exempt exactly as
before.

Pure Python -- no real ``resolve_roster()`` call in any test here; each case
monkeypatches the seam directly, matching the sibling suite's own
identity-resolution-patching convention (see that file's own docstring).

Spec backlink: pln-deny-unenumerated-agent-types-e56d1b § C2 / AC5
"""

from __future__ import annotations

from coordinator_core.bash_guards import (
    block_reviewer_bash_outside_allowlist as guard,
)

_INVENTED_TYPE = "hookprobe-named"
_ENUMERATED_NON_CONFINED_TYPE = "coordinator:enricher"
_CONFINED_TYPE = "coordinator:code-reviewer"


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


def _wire_identity(monkeypatch, subagent_type=""):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id: subagent_type,
    )


# ---------------------------------------------------------------------------
# _helpers.is_confined_by_roster_absence -- unit-level
# ---------------------------------------------------------------------------


def test_roster_absence_empty_type_never_confines(monkeypatch):
    from coordinator_core.bash_guards import _helpers

    # No resolve_roster call should even be attempted for a falsy leg --
    # patch it to raise if reached, proving the empty-string short-circuit.
    def _boom():
        raise AssertionError("resolve_roster() must not be called for an empty leg")

    monkeypatch.setattr(_helpers, "resolve_roster", _boom)
    assert _helpers.is_confined_by_roster_absence("") is False
    assert _helpers.is_confined_by_roster_absence(None) is False


def test_roster_absence_true_for_unenumerated_type(monkeypatch):
    from coordinator_core.bash_guards import _helpers

    monkeypatch.setattr(
        _helpers, "resolve_roster", lambda: (frozenset({"coordinator:enricher"}), None)
    )
    assert _helpers.is_confined_by_roster_absence(_INVENTED_TYPE) is True


def test_roster_absence_false_for_enumerated_type(monkeypatch):
    from coordinator_core.bash_guards import _helpers

    monkeypatch.setattr(
        _helpers, "resolve_roster", lambda: (frozenset({"coordinator:enricher"}), None)
    )
    assert _helpers.is_confined_by_roster_absence("coordinator:enricher") is False


def test_roster_absence_fails_closed_on_roster_load_failure(monkeypatch):
    from coordinator_core.bash_guards import _helpers

    monkeypatch.setattr(
        _helpers, "resolve_roster", lambda: (None, "roster source unreadable")
    )
    assert _helpers.is_confined_by_roster_absence(_INVENTED_TYPE) is True


# ---------------------------------------------------------------------------
# check() -- full-guard integration: an invented type is now confined
# ---------------------------------------------------------------------------


def test_unenumerated_type_denies_arbitrary_command(monkeypatch):
    _wire_identity(monkeypatch, subagent_type=_INVENTED_TYPE)
    monkeypatch.setattr(guard, "is_confined_by_roster_absence", lambda t: t == _INVENTED_TYPE)
    payload = _payload("rm -rf /", agent_type=None)
    verdict = guard.check(payload)
    assert verdict is not None
    assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_unenumerated_type_allows_readonly_tier_a_command(monkeypatch):
    _wire_identity(monkeypatch, subagent_type=_INVENTED_TYPE)
    monkeypatch.setattr(guard, "is_confined_by_roster_absence", lambda t: t == _INVENTED_TYPE)
    payload = _payload("git status", agent_type=None)
    assert guard.check(payload) is None


def test_enumerated_non_confined_type_still_allows(monkeypatch):
    """AC5's negative spec: a type ON the roster but not in the confined
    set (``coordinator:enricher``) must keep its pre-C2 unrestricted Bash
    surface -- the new leg must never widen confinement to every enumerated
    type, only to unenumerated ones."""
    _wire_identity(monkeypatch, subagent_type="")
    monkeypatch.setattr(guard, "is_confined_by_roster_absence", lambda t: False)
    payload = _payload("rm -rf /", agent_type=_ENUMERATED_NON_CONFINED_TYPE)
    assert guard.check(payload) is None


def test_already_confined_type_unaffected_by_roster_leg(monkeypatch):
    """The pre-existing confined type must still be confined even when the
    roster-absence leg says "not confined" for every leg it is asked about
    -- proves the OR composition: the cheaper ``is_confined_findings_agent``
    leg alone is sufficient and the new leg never NARROWS confinement."""
    _wire_identity(monkeypatch, subagent_type=_CONFINED_TYPE)
    monkeypatch.setattr(guard, "is_confined_by_roster_absence", lambda t: False)
    payload = _payload("rm -rf /", agent_type=None)
    verdict = guard.check(payload)
    assert verdict is not None
    assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"
