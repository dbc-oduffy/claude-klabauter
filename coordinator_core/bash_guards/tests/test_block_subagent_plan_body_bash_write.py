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

from coordinator_core.bash_guards import _verdict
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
    assert ".coordinator-local/subagent-share/<path>.md" in reason


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


def _ps_payload(command, **kw):
    p = _payload(command, **kw)
    p["tool_name"] = "PowerShell"
    return p


def test_powershell_redirect_idiom_still_fires_dialect_neutral(monkeypatch):
    """`>`/`>>` is the SAME operator in PowerShell as POSIX -- idiom (1)
    needs no PowerShell-specific matcher and keeps ruling correctly, still
    as the ADVISORY_REWRITE allow+additionalContext shape.
    """
    _stub(monkeypatch, resolved_agent_id="deadbeef0123", subagent_type="coordinator:executor")
    payload = _ps_payload('"in progress" >> docs/plans/2026-07-30-x.md')
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_powershell_cmdlet_write_records_silent_not_clean(monkeypatch):
    """A PowerShell cmdlet write this guard has no verb table for
    (`Set-Content`) must not read as a confirmed clean verdict -- it records
    SILENT on the out-of-band channel while still returning `None` (the
    fail-open default), per AC1/AC3.
    """
    _stub(monkeypatch, resolved_agent_id="deadbeef0123", subagent_type="coordinator:executor")
    payload = _ps_payload("Set-Content docs/plans/2026-07-30-x.md -Value 'x'")
    with _verdict.collecting() as silences:
        result = guard.check(payload)
    assert result is None
    assert _verdict.was_silent("block_subagent_plan_body_bash_write", silences)


def test_unenumerated_type_falls_through_to_advisory(monkeypatch):
    """AC6/C3 -- the SAME identity gate's Case 2 (kind resolves CLEANLY to
    something absent from C1's roster) no longer exits as allow; it falls
    through to the SAME target-detection axis coordinator:executor reaches,
    landing on the same ADVISORY_REWRITE allow+additionalContext shape --
    no more trust than coordinator:executor for this guard's purposes.
    """
    _stub(monkeypatch, resolved_agent_id="deadbeef0123", subagent_type="hookprobe-named")
    monkeypatch.setattr(
        guard, "resolve_roster", lambda: (frozenset({"coordinator:enricher"}), None)
    )
    payload = _payload(_WRITE_CMD)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "additionalContext" in result["hookSpecificOutput"]


def test_enumerated_non_executor_type_still_allows_silently(monkeypatch):
    """Case 2, positive direction -- a kind that resolves cleanly to
    something ON the roster keeps today's silent allow, unaffected by the
    narrowing.
    """
    _stub(monkeypatch, resolved_agent_id="deadbeef0123", subagent_type="coordinator:enricher")
    monkeypatch.setattr(
        guard, "resolve_roster", lambda: (frozenset({"coordinator:enricher"}), None)
    )
    payload = _payload(_WRITE_CMD)
    assert guard.check(payload) is None


def test_roster_load_error_falls_back_to_allow(monkeypatch):
    """A roster-load failure is a peer-repo hiccup, not this guard's problem
    to newly deny on (C1's PreToolUse(Agent) deny is the primary fix) --
    falls back to today's allow rather than denying.
    """
    _stub(monkeypatch, resolved_agent_id="deadbeef0123", subagent_type="hookprobe-named")
    monkeypatch.setattr(guard, "resolve_roster", lambda: (None, "roster unresolved"))
    payload = _payload(_WRITE_CMD)
    assert guard.check(payload) is None


def test_powershell_non_executor_kind_no_silent_no_deep_scan(monkeypatch):
    """Identity axis still gates first: a non-executor kind never reaches
    the PowerShell target-detection leg at all, so no SILENT is recorded.
    """
    _stub(monkeypatch, resolved_agent_id="deadbeef0123", subagent_type="coordinator:enricher")
    payload = _ps_payload("Set-Content docs/plans/2026-07-30-x.md -Value 'x'")
    with _verdict.collecting() as silences:
        result = guard.check(payload)
    assert result is None
    assert silences == []
