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

Divergence 18 (2026-08-14) tests appended below this original suite --
same file, since they exercise the identical named-vs-unnamed identity
resolution seam, this time for WHETHER confinement fires (check()'s
is_confined computation) rather than WHICH ruleset a confined identity
gets. See that section's own docstrings and
coordinator_core/bash_guards/block_reviewer_bash_outside_allowlist.py §
Divergence 18 / the new docs/problems entry for the full mechanism.
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


# ---------------------------------------------------------------------------
# Unnamed dispatch -- unaffected (no regression).
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# coordinator:code-reviewer parity -- byte-identical, unaffected by this fix.
# ---------------------------------------------------------------------------


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
        guard, "_read_backpointer_subagent_type", lambda git_root, agent_id, **kw: ""
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


# ---------------------------------------------------------------------------
# Divergence 18 (2026-08-14) -- close the confinement-manufacturing residual:
# a named dispatch whose back-pointer-resolved subagent_type is a KNOWN,
# NON-confined type must not be confined at all, regardless of the
# caller-chosen teammate name landing on agent_type.
# ---------------------------------------------------------------------------


def _wire_named_dispatch_enumerated(monkeypatch, subagent_type):
    """Like _wire_named_dispatch, but the roster recognises
    coordinator:git-commit-agent as enumerated-but-not-confined -- the shape
    Divergence 18's repro needs (a real, known, non-confined type)."""
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
    """The confirmed live repro: a named dispatch whose subagent_type
    resolves to coordinator:git-commit-agent (known, not confined) must
    allow scoped-git-commit -- check() returns None."""
    _wire_named_dispatch_enumerated(monkeypatch, subagent_type=_GIT_COMMIT_AGENT_TYPE)
    payload = _payload(
        "scoped-git-commit --pathspec foo.py -m x", agent_type=_TEAMMATE_NAME
    )
    _assert_allowed(guard.check(payload))


def test_named_unnamed_parity_for_non_confined_type(monkeypatch):
    """Named/unnamed parity differential: for a given non-confined type, the
    confinement verdict must be identical whether agent_type carries the
    real type directly (unnamed shape) or a teammate name with the type on
    subagent_type (named shape)."""
    command = "scoped-git-commit --pathspec foo.py -m x"

    # Unnamed shape: agent_type IS the real type, subagent_type empty.
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

    # Named shape: teammate name on agent_type, real type on subagent_type.
    _wire_named_dispatch_enumerated(monkeypatch, subagent_type=_GIT_COMMIT_AGENT_TYPE)
    named_payload = _payload(command, agent_type=_TEAMMATE_NAME)
    named_result = guard.check(named_payload)

    assert unnamed_result is None
    assert named_result is None
    assert unnamed_result == named_result


def test_named_dispatch_unresolvable_subagent_type_still_fail_closed(monkeypatch):
    """Fail-closed regression: a teammate name with NO resolvable
    subagent_type (empty string, back-pointer chain unresolved) must still
    be denied -- confinement still falls through to the original OR, and
    leg 3 (roster absence) still confines the raw name."""
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
    """Confined-type regression: a teammate name with subagent_type =
    coordinator:code-reviewer must still be denied -- a name cannot launder
    a confined type into freedom, and the fix cannot free it either."""
    _wire_named_dispatch(monkeypatch, subagent_type=_REVIEWER_TYPE)
    payload = _payload(
        "scoped-git-commit --pathspec foo.py -m x", agent_type=_TEAMMATE_NAME
    )
    _assert_denied(guard.check(payload))


def test_backpointer_expected_session_id_forwarded_from_payload(monkeypatch):
    """(2026-08-14, review finding) check() must forward the payload's own
    session_id as expected_em_session_id -- pins the call shape, not just
    the mocked outcome, so a regression that drops the kwarg is caught even
    though the other tests in this file stub the function permissively."""
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
