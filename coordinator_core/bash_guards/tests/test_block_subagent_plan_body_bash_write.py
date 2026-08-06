"""Tests for coordinator_core.bash_guards.block_subagent_plan_body_bash_write.

No dedicated test file existed for this guard before the 2026-07-30 dispatch
that added this one (it was only exercised incidentally by other guards'
test corpora -- see test_confinement_attack_corpus.py, test_cd_prefix_bypass.py,
test_alternative_liveness_gate.py).

History (same day, two rounds): an EARLIER pass confined an unresolvable kind
here exactly like block_subagent_commit/block_subagent_destructive_action do
-- REVERTED once it was pointed out this guard's write-side sibling
(write_guards.block_subagent_plan_body_write) carries an explicit
2026-06-09 PM ruling (lookup-fail-is-allow, "don't punish legitimate
integrator/enricher work on infra noise"), and a SECOND revert-scoping
mistake initially reverted only the write-side sibling, leaving this Bash
guard confining while its Write-side twin allowed -- a split that never
existed pre-dispatch, since both guards encode ONE rule ("who may edit a
plan body") across two surfaces. This file now pins the FINAL, correct
state: lookup-miss allows on BOTH surfaces, and the measurement-only
kind-resolution-failure signal still fires (with an ALLOWED disposition,
not a guessed one) so a future, properly-scoped PM conversation about the
2026-06-09 ruling has real frequency data.

Pure Python -- no shell spawns, no filesystem writes. Identity resolution
is monkeypatched directly onto the guard module object.
"""

from __future__ import annotations

from coordinator_core.bash_guards import block_subagent_plan_body_bash_write as guard


def _payload(command, agent_id="deadbeef0123", session_id="sess1", cwd="/repo"):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": session_id,
        "cwd": cwd,
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    return p


_WRITE_CMD = 'echo "in progress" >> docs/plans/2026-07-30-x.md'


def _stub(monkeypatch, resolved_agent_id="deadbeef0123", subagent_type=""):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: resolved_agent_id
    )
    monkeypatch.setattr(
        guard, "_read_backpointer_subagent_type", lambda git_root, agent_id: subagent_type
    )
    monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)


def test_em_caller_no_agent_id_allowed_and_silent(monkeypatch, capsys):
    """No agent_id at all -> EM main-loop -> always allow, and the
    instrumentation signal (about SUBAGENT kind-resolution failures
    specifically) must not fire for a non-subagent caller.
    """
    _stub(monkeypatch, resolved_agent_id="", subagent_type="")
    payload = _payload(_WRITE_CMD, agent_id=None)
    assert guard.check(payload) is None
    assert capsys.readouterr().err == ""


def test_unresolvable_kind_still_allows(monkeypatch, capsys):
    """raw agent_id present, but the backpointer chain lookup misses
    (unreadable/missing) -- allows (matches the write-side sibling and the
    2026-06-09 PM ruling both guards encode), and the measurement-only
    signal still fires, reporting its true ALLOWED disposition.
    """
    _stub(monkeypatch, resolved_agent_id="deadbeef0123", subagent_type="")
    payload = _payload(_WRITE_CMD)
    assert guard.check(payload) is None
    err = capsys.readouterr().err
    assert "kind-resolution-failed" in err
    assert "ALLOWS" in err


def test_unparseable_agent_id_still_allows(monkeypatch):
    """raw agent_id present but fails to canonicalize (unrecognised shape) --
    still allows, same as any other kind-resolution-failure shape.
    """
    _stub(monkeypatch, resolved_agent_id="", subagent_type="")
    payload = _payload(_WRITE_CMD)
    assert guard.check(payload) is None


def test_resolvable_non_executor_kind_allowed_and_silent(monkeypatch, capsys):
    """A resolvable kind that isn't coordinator:executor allows, and does
    not trip the kind-UNRESOLVED instrumentation (its kind resolved fine --
    it just isn't executor).
    """
    _stub(monkeypatch, resolved_agent_id="deadbeef0123", subagent_type="coordinator:enricher")
    payload = _payload(_WRITE_CMD)
    assert guard.check(payload) is None
    assert capsys.readouterr().err == ""


def test_resolvable_executor_kind_still_fires_now_advisory(monkeypatch):
    """A resolved coordinator:executor kind still fires the guard, but as
    ADVISORY_REWRITE (C14c): allow + additionalContext, not deny.
    """
    _stub(monkeypatch, resolved_agent_id="deadbeef0123", subagent_type="coordinator:executor")
    payload = _payload(_WRITE_CMD)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    reason = result["hookSpecificOutput"]["additionalContext"]
    assert "coordinator:executor" in reason
    assert "state/subagent-share/<path>.md" in reason


def test_resolvable_ambiguous_kind_still_fires_unconditionally(monkeypatch):
    """AMBIGUOUS collision sentinel keeps its own unconditional fire branch,
    independent of the lookup-fail-is-allow default -- now advisory-allow
    rather than deny (C14c).
    """
    _stub(monkeypatch, resolved_agent_id="deadbeef0123", subagent_type="AMBIGUOUS")
    payload = _payload("cat docs/plans/2026-07-30-x.md")
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_non_write_command_allows_even_with_unresolved_kind(monkeypatch):
    """TARGET-DETECTION axis is independent of identity: an unresolved kind
    still allows through a command with no unambiguous write idiom (a plain
    read), matching the guard's own "any doubt -> allow" contract.
    """
    _stub(monkeypatch, resolved_agent_id="deadbeef0123", subagent_type="")
    payload = _payload("cat docs/plans/2026-07-30-x.md")
    assert guard.check(payload) is None
